"""证券结算与企业行动处理领域规则与状态转换。"""
from datetime import date
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, iso_date, number, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {
    'apply_corporate': {'corporate_actions'},
    'approve': {'settlement_officer'},
    'settle': {'settlement_officer'},
    'fail': {'settlement_officer'},
    'replenish': {'settlement_officer'},
    'escalate_compensation': {'settlement_officer'},
    'confirm_compensation': {'settlement_officer'},
    'reverse': {'corporate_actions', 'settlement_officer'},
}
TRANSITIONS = {
    'apply_corporate': {'captured': 'adjusted'},
    'approve': {'captured': 'approved', 'adjusted': 'approved'},
    'settle': {'approved': 'settled'},
    'fail': {'approved': 'failed'},
    'escalate_compensation': {'failed': 'compensation_pending'},
    'confirm_compensation': {'compensation_pending': 'compensation_pending'},
    'reverse': {'settled': 'reversed', 'failed': 'reversed', 'compensation_pending': 'reversed'},
}

# 补券任务状态
TASK_OPEN = "open"                    # 补券中，缺口尚未补够
TASK_COMPLETED = "completed"          # 截止日前分批补够，原指令恢复交收
TASK_BREACHED = "breached"            # 截止日未补足，违约认定
TASK_COMPENSATED = "compensated"      # 赔付已确认，可冲正
TASK_CLOSED_STATES = {TASK_COMPLETED, TASK_BREACHED, TASK_COMPENSATED}

# 仅一条未关闭任务的状态集合
TASK_UNCLOSED_STATES = {TASK_OPEN, TASK_BREACHED}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    TASK_OPEN = TASK_OPEN
    TASK_COMPLETED = TASK_COMPLETED
    TASK_BREACHED = TASK_BREACHED
    TASK_COMPENSATED = TASK_COMPENSATED

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

    def required_quantity(self, payload: Dict[str, Any]) -> int:
        return int(payload.get("effective_quantity", payload.get("adjusted_quantity", payload["quantity"])))

    def settlement_price(self, payload: Dict[str, Any]) -> float:
        return float(payload.get("effective_price", payload.get("adjusted_price", payload["price"])))

    def validate_fail_input(self, data: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        """失败处置：记录原因并生成补券任务所需的责任卖方、应补数量和截止日。"""
        data = dict(data or {})
        text(data, "fail_reason")
        text(data, "responsible_seller")
        required = self.required_quantity(payload)
        shortfall = integer(data, "shortfall_quantity", 1, required)
        iso_date(data, "due_date")
        return data

    def validate_replenish_input(self, data: Dict[str, Any], task: Dict[str, Any]) -> Tuple[int, str]:
        """一次补券数量必须为正且不超过当前缺口。"""
        data = dict(data or {})
        quantity = integer(data, "quantity", 1)
        outstanding = int(task["required_quantity"]) - int(task["replenished_quantity"])
        if quantity > outstanding:
            raise ValidationError("补券数量超过待补缺口%s" % outstanding)
        note = text(data, "note") if str(data.get("note", "")).strip() else ""
        return quantity, note

    def breach_value(self, task: Dict[str, Any], payload: Dict[str, Any]) -> float:
        outstanding = int(task["required_quantity"]) - int(task["replenished_quantity"])
        return round(outstanding * self.settlement_price(payload), 2)

    def can_escalate(self, task: Dict[str, Any], today: date) -> bool:
        if task["status"] != TASK_OPEN:
            return False
        due = date.fromisoformat(task["due_date"])
        return today > due

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
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
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败：%s" % changes["fail_reason"]
        elif action == "escalate_compensation":
            changes["penalty_amount"] = number(data, "penalty_amount", 0)
            changes["outstanding_quantity"] = integer(data, "outstanding_quantity", 0)
            summary = "截止日未补足，按未补市值认定违约，停在待赔付"
        elif action == "confirm_compensation":
            changes["compensation_confirmed"] = True
            changes["compensation_amount"] = number(data, "compensation_amount", 0)
            summary = "赔付已确认"
        elif action == "reverse":
            if record["state"] == "compensation_pending" and not p.get("compensation_confirmed"):
                raise Conflict("赔付确认后才能冲正")
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
