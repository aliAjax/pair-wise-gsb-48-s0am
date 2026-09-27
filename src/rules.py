"""证券结算与企业行动处理领域规则与状态转换。"""
from datetime import date
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {
    'apply_corporate': {'corporate_actions'},
    'approve': {'settlement_officer'},
    'settle': {'settlement_officer'},
    'fail': {'settlement_officer'},
    'make_up_delivery': {'settlement_officer'},
    'mark_breach': {'settlement_officer'},
    'confirm_compensation': {'settlement_officer'},
    'reverse': {'corporate_actions', 'settlement_officer'},
}
TRANSITIONS = {
    'apply_corporate': {'captured': 'adjusted'},
    'approve': {'captured': 'approved', 'adjusted': 'approved'},
    'settle': {'approved': 'settled'},
    'fail': {'approved': 'make_up'},
    'make_up_delivery': {'make_up': 'make_up'},
    'mark_breach': {'make_up': 'pending_compensation'},
    'confirm_compensation': {'pending_compensation': 'pending_compensation'},
    'reverse': {'settled': 'reversed', 'failed': 'reversed', 'pending_compensation': 'reversed'},
}
# 补券批次状态：未关闭=可继续补券/可认定违约；补足或赔付确认后关闭。
OPEN_TASK_STATUS = 'open'
BREACHED_TASK_STATUS = 'breached'
FULFILLED_TASK_STATUS = 'fulfilled'
COMPENSATED_TASK_STATUS = 'compensated'
UNCLOSED_TASK_STATUS = (OPEN_TASK_STATUS, BREACHED_TASK_STATUS)
DEFAULT_PENALTY_RATE = 0.0005


def parse_due_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("截止日必须为YYYY-MM-DD格式") from exc


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", ["none", "split", "dividend", "merger"])
        number(p, "action_ratio", 0.01)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(p["action_ratio"]), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                if item["payload"].get("side") == payload.get("side") and item["payload"].get("quantity") == payload.get("quantity") and item["payload"].get("price") == payload.get("price"):
                    raise Conflict("疑似重复结算指令")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    @staticmethod
    def required_quantity(payload: Dict[str, Any]) -> int:
        return int(payload.get("effective_quantity") or payload.get("adjusted_quantity") or payload["quantity"])

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        data = dict(data or {})
        if action in FAILURE_ACTIONS:
            raise Conflict("%s需通过失败处置流程执行" % action)
        new_state = self.require_transition(record, action)
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "apply_corporate":
            if p["corporate_action"] == "none":
                raise ValidationError("没有待处理的公司行动")
            changes["corporate_applied"] = True
            changes["effective_quantity"] = p["adjusted_quantity"]
            changes["effective_price"] = p["adjusted_price"]
            summary = "公司行动已应用"
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            required_quantity = self.required_quantity(p)
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            summary = "交收完成"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ------------------------------------------------------------------
    # 失败处置：失败留痕 -> 补券任务 -> 分批补券 -> 违约/赔付 -> 冲正
    # ------------------------------------------------------------------

    def open_fail_task(self, record: Dict[str, Any], data: Dict[str, Any], today: Optional[date] = None) -> Tuple[str, Dict[str, Any], Dict[str, Any], str]:
        """交收失败：记录原因并生成补券任务，指令进入补券中。"""
        new_state = self.require_transition(record, "fail")
        data = dict(data or {})
        today = today or date.today()
        reason = text(data, "fail_reason")
        responsible_seller = text(data, "responsible_seller")
        due_date = parse_due_date(text(data, "due_date"))
        if due_date < today:
            raise ValidationError("截止日不能早于当前日期")
        required_quantity = self.required_quantity(record["payload"])
        shortage = integer(data, "shortage_quantity", 1, required_quantity) if "shortage_quantity" in data else required_quantity
        breach_price = number(data, "breach_price", 0.01) if "breach_price" in data else float(record["payload"].get("effective_price") or record["payload"]["price"])
        penalty_rate = number(data, "penalty_rate", 0, 1) if "penalty_rate" in data else DEFAULT_PENALTY_RATE
        p = dict(record["payload"])
        p["fail_reason"] = reason
        task = {
            "status": OPEN_TASK_STATUS,
            "responsible_seller": responsible_seller,
            "required_quantity": required_quantity,
            "shortage_quantity": shortage,
            "delivered_quantity": 0,
            "due_date": due_date.isoformat(),
            "breach_price": round(breach_price, 4),
            "penalty_rate": penalty_rate,
            "penalty_days": 0,
            "breach_amount": 0.0,
            "compensation_confirmed": False,
        }
        summary = "交收失败：%s；已生成补券任务，责任卖方%s，应补%s，截止%s" % (reason, responsible_seller, shortage, task["due_date"])
        return new_state, p, task, summary

    def apply_make_up_delivery(self, record: Dict[str, Any], task: Dict[str, Any], data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any], str]:
        """登记一笔补券，分批累计；补足后指令回到approved继续交收。"""
        new_state = self.require_transition(record, "make_up_delivery")
        data = dict(data or {})
        quantity = integer(data, "delivered_quantity", 1)
        outstanding = int(task["shortage_quantity"]) - int(task["delivered_quantity"])
        if quantity > outstanding:
            raise ValidationError("补券数量超过未补缺口%s" % outstanding)
        task = dict(task)
        task["delivered_quantity"] = int(task["delivered_quantity"]) + quantity
        remaining = int(task["shortage_quantity"]) - int(task["delivered_quantity"])
        p = dict(record["payload"])
        p["make_up_delivered_quantity"] = task["delivered_quantity"]
        p["make_up_remaining_quantity"] = remaining
        fulfilled = remaining == 0
        if fulfilled:
            task["status"] = FULFILLED_TASK_STATUS
            task["closed_at_note"] = "补券补足"
            new_state = "approved"
            summary = "补券已补足（累计%s），原指令可继续交收" % task["delivered_quantity"]
        else:
            summary = "收到补券%s，累计%s，尚缺%s" % (quantity, task["delivered_quantity"], remaining)
        return new_state, p, task, summary

    def mark_breach(self, record: Dict[str, Any], task: Dict[str, Any], data: Dict[str, Any], today: Optional[date] = None) -> Tuple[str, Dict[str, Any], Dict[str, Any], str]:
        """截止日仍未补足：按未补市值计违约金，停在待赔付。"""
        new_state = self.require_transition(record, "mark_breach")
        data = dict(data or {})
        today = today or date.today()
        due_date = parse_due_date(str(task["due_date"]))
        remaining = int(task["shortage_quantity"]) - int(task["delivered_quantity"])
        if remaining <= 0:
            raise Conflict("缺口已补足，不能认定违约")
        if today <= due_date:
            raise Conflict("尚未到截止日，不能认定违约")
        penalty_days = integer(data, "penalty_days", 1) if "penalty_days" in data else (today - due_date).days
        market_value = round(remaining * float(task["breach_price"]), 2)
        breach_amount = round(market_value * float(task["penalty_rate"]) * penalty_days, 2)
        task = dict(task)
        task["status"] = BREACHED_TASK_STATUS
        task["penalty_days"] = penalty_days
        task["breach_amount"] = breach_amount
        p = dict(record["payload"])
        p["breach_market_value"] = market_value
        p["breach_amount"] = breach_amount
        p["breach_remaining_quantity"] = remaining
        summary = "截止日未补足，未补%s股市值%s，违约金%s（费率%s×%s天），停在待赔付" % (
            remaining, market_value, breach_amount, task["penalty_rate"], penalty_days,
        )
        return new_state, p, task, summary

    def confirm_compensation(self, record: Dict[str, Any], task: Dict[str, Any], data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any], str]:
        """确认赔付到账：任务关闭，指令方可冲正（状态保持待赔付）。"""
        self.require_transition(record, "confirm_compensation")
        if task.get("compensation_confirmed"):
            raise Conflict("赔付已确认，请勿重复操作")
        data = dict(data or {})
        reference = text(data, "compensation_reference")
        task = dict(task)
        task["status"] = COMPENSATED_TASK_STATUS
        task["compensation_confirmed"] = True
        task["compensation_reference"] = reference
        p = dict(record["payload"])
        p["compensation_confirmed"] = True
        p["compensation_reference"] = reference
        summary = "赔付%s已确认到账（凭证%s），可执行冲正" % (task["breach_amount"], reference)
        return record["state"], p, task, summary

    def check_reverse_allowed(self, record: Dict[str, Any]) -> None:
        if record["state"] == "pending_compensation" and not record["payload"].get("compensation_confirmed"):
            raise Conflict("赔付未确认，不能冲正")


FAILURE_ACTIONS = {"fail", "make_up_delivery", "mark_breach", "confirm_compensation"}
