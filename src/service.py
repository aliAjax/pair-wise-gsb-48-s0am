"""业务用例编排、权限检查与审计。"""
from datetime import date
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, number, text, ValidationError
from .repository import Repository
from .rules import (
    TASK_BREACHED,
    TASK_COMPLETED,
    TASK_OPEN,
    DomainRules,
)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _task_view(task: Optional[Dict[str, Any]], lots: List[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if task is None:
            return None
        required = int(task["required_quantity"])
        replenished = int(task["replenished_quantity"])
        view = {
            "id": task["id"],
            "status": task["status"],
            "fail_reason": task["fail_reason"],
            "responsible_seller": task["responsible_seller"],
            "required_quantity": required,
            "replenished_quantity": replenished,
            "outstanding_quantity": max(0, required - replenished),
            "due_date": task["due_date"],
            "penalty_amount": task["penalty_amount"],
            "compensation_amount": task["compensation_amount"],
            "created_at": task["created_at"],
            "closed_at": task["closed_at"],
        }
        if lots is not None:
            view["lots"] = lots
        return view

    def _attach_task(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record["replenishment"] = self._task_view(self.repository.replenishment_overview(record["id"]))
        return record

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
        record["replenishment"] = None
        return record

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        tasks = self.repository.latest_tasks_by_records([record["id"] for record in records])
        for record in records:
            record["replenishment"] = self._task_view(tasks.get(record["id"]))
        return records

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._attach_task(self.repository.get(record_id))

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        data = data or {}
        if action == "fail":
            result = self._fail(actor, record, int(expected_version), data)
        elif action == "replenish":
            result = self._replenish(actor, record, int(expected_version), data)
        elif action == "escalate_compensation":
            result = self._escalate(actor, record, int(expected_version), data)
        elif action == "confirm_compensation":
            result = self._confirm_compensation(actor, record, int(expected_version), data)
        else:
            new_state, new_payload, summary = self.rules.apply_action(record, action, data)
            result = self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
            )
        return self._attach_task(result)

    def _fail(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        # 先校验状态机（approved -> failed），再校验失败处置必填项
        self.rules.require_transition(record, "fail")
        validated = self.rules.validate_fail_input(data, record["payload"])
        fail_reason = validated["fail_reason"]
        responsible_seller = validated["responsible_seller"]
        shortfall = int(validated["shortfall_quantity"])
        due_date = validated["due_date"]
        new_state, new_payload, summary = self.rules.apply_action(record, "fail", validated)
        updated, task = self.repository.fail_with_task(
            record_id=record["id"],
            expected_version=expected_version,
            payload=new_payload,
            actor_id=actor.user_id,
            fail_reason=fail_reason,
            responsible_seller=responsible_seller,
            shortfall_quantity=shortfall,
            due_date=due_date,
            details={
                "summary": summary,
                "from": record["state"],
                "to": new_state,
                "responsible_seller": responsible_seller,
                "required_quantity": shortfall,
                "due_date": due_date,
            },
        )
        return updated

    def _replenish(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        if record["state"] != "failed":
            raise Conflict("只有交收失败的指令可以补券")
        task = self.repository.get_open_task(record["id"])
        if task is None or task["status"] != TASK_OPEN:
            raise Conflict("该指令没有进行中的补券任务")
        quantity, note = self.rules.validate_replenish_input(data, task)
        updated, task, lot = self.repository.apply_replenishment(
            record_id=record["id"],
            expected_version=expected_version,
            quantity=quantity,
            note=note,
            actor_id=actor.user_id,
        )
        return updated

    def _escalate(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        if record["state"] != "failed":
            raise Conflict("只有交收失败的指令可以认定违约")
        task = self.repository.get_open_task(record["id"])
        if task is None or task["status"] != TASK_OPEN:
            raise Conflict("该指令没有进行中的补券任务")
        today_text = text(data, "today") if str(data.get("today", "")).strip() else None
        try:
            today = date.fromisoformat(today_text) if today_text else date.today()
        except ValueError as exc:
            raise ValidationError("today必须是YYYY-MM-DD格式日期") from exc
        if not self.rules.can_escalate(task, today):
            raise Conflict("截止日未到或缺口已补够，不能认定违约")
        outstanding = int(task["required_quantity"]) - int(task["replenished_quantity"])
        breach_value = self.rules.breach_value(task, record["payload"])
        if data.get("penalty_amount") is None:
            penalty = breach_value
        else:
            penalty = number(data, "penalty_amount", 0)
        action_data = {"penalty_amount": penalty, "outstanding_quantity": outstanding}
        new_state, new_payload, summary = self.rules.apply_action(record, "escalate_compensation", action_data)
        updated, task = self.repository.escalate_compensation(
            record_id=record["id"],
            expected_version=expected_version,
            payload=new_payload,
            penalty_amount=penalty,
            outstanding_quantity=outstanding,
            actor_id=actor.user_id,
            details={
                "summary": summary,
                "from": record["state"],
                "to": new_state,
                "responsible_seller": task["responsible_seller"],
                "outstanding_quantity": outstanding,
                "breach_value": breach_value,
                "penalty_amount": penalty,
                "due_date": task["due_date"],
            },
        )
        return updated

    def _confirm_compensation(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        if record["state"] != "compensation_pending":
            raise Conflict("只有待赔付的指令可以确认赔付")
        task = self.repository.get_open_task(record["id"])
        if task is None or task["status"] != TASK_BREACHED:
            raise Conflict("该指令没有待赔付的补券任务")
        penalty_amount = float(task["penalty_amount"] or 0.0)
        if data.get("compensation_amount") is None:
            compensation = penalty_amount
        else:
            compensation = number(data, "compensation_amount", 0)
        if compensation < penalty_amount:
            raise ValidationError("赔付金额不能低于违约金%s" % penalty_amount)
        action_data = {"compensation_amount": compensation}
        new_state, new_payload, summary = self.rules.apply_action(record, "confirm_compensation", action_data)
        updated, task = self.repository.confirm_compensation(
            record_id=record["id"],
            expected_version=expected_version,
            payload=new_payload,
            compensation_amount=compensation,
            actor_id=actor.user_id,
            details={
                "summary": summary,
                "from": record["state"],
                "to": new_state,
                "compensation_amount": compensation,
                "penalty_amount": penalty_amount,
            },
        )
        return updated

    def replenishment_detail(self, actor: Actor, record_id: int) -> Optional[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        task = self.repository.replenishment_overview(record_id)
        if task is None:
            return None
        lots = self.repository.list_lots(int(task["id"]))
        return self._task_view(task, lots)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
