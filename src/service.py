"""业务用例编排、权限检查与审计。"""
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, text
from .repository import Repository
from .rules import FAILURE_ACTIONS, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, clock: Callable[[], date] = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.clock = clock or date.today

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        if action in FAILURE_ACTIONS:
            return self._handle_failure_action(actor, record, int(expected_version), action, data or {})
        if action == "reverse":
            self.rules.check_reverse_allowed(record)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record["id"],
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def _handle_failure_action(self, actor: Actor, record: Dict[str, Any], expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if action == "fail":
            return self._fail(actor, record, expected_version, data)
        if action == "confirm_compensation":
            tasks = self.repository.list_tasks(record["id"])
            if not tasks:
                raise NotFound("该指令没有补券任务")
            return self._confirm_compensation(actor, record, tasks[-1], expected_version, data)
        task = self.repository.get_open_task(record["id"])
        if task is None:
            raise NotFound("该指令没有未关闭的补券任务")
        if action == "make_up_delivery":
            return self._make_up_delivery(actor, record, task, expected_version, data)
        if action == "mark_breach":
            return self._mark_breach(actor, record, task, expected_version, data)
        raise PermissionDenied("未知失败处置动作")

    def _fail(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        if self.repository.get_open_task(record["id"]) is not None:
            raise Conflict("该指令已存在未关闭的补券任务")
        new_state, new_payload, task, summary = self.rules.open_fail_task(record, data, today=self.clock())
        details = {
            "summary": summary,
            "input": data,
            "from": record["state"],
            "to": new_state,
            "task": {
                "responsible_seller": task["responsible_seller"],
                "required_quantity": task["required_quantity"],
                "shortage_quantity": task["shortage_quantity"],
                "due_date": task["due_date"],
            },
        }
        result = self.repository.open_task(record["id"], expected_version, new_state, new_payload, task, actor.user_id, details)
        return result["record"]

    def _make_up_delivery(self, actor: Actor, record: Dict[str, Any], task: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        new_state, new_payload, new_task, summary = self.rules.apply_make_up_delivery(record, task, data)
        quantity = int(data["delivered_quantity"])
        details = {
            "summary": summary,
            "input": {"delivered_quantity": quantity},
            "from": record["state"],
            "to": new_state,
            "task_id": task["id"],
            "task_status": new_task["status"],
            "delivered_total": new_task["delivered_quantity"],
            "remaining": new_task["shortage_quantity"] - new_task["delivered_quantity"],
        }
        result = self.repository.save_task_and_record(
            record["id"], expected_version, new_state, new_payload, task["id"], new_task,
            actor.user_id, "make_up_delivery", details, add_batch_quantity=quantity,
        )
        return result["record"]

    def _mark_breach(self, actor: Actor, record: Dict[str, Any], task: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        new_state, new_payload, new_task, summary = self.rules.mark_breach(record, task, data, today=self.clock())
        details = {
            "summary": summary,
            "input": data,
            "from": record["state"],
            "to": new_state,
            "task_id": task["id"],
            "remaining_quantity": new_payload["breach_remaining_quantity"],
            "breach_market_value": new_payload["breach_market_value"],
            "breach_amount": new_task["breach_amount"],
            "penalty_days": new_task["penalty_days"],
        }
        result = self.repository.save_task_and_record(
            record["id"], expected_version, new_state, new_payload, task["id"], new_task,
            actor.user_id, "mark_breach", details,
        )
        return result["record"]

    def _confirm_compensation(self, actor: Actor, record: Dict[str, Any], task: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        new_state, new_payload, new_task, summary = self.rules.confirm_compensation(record, task, data)
        details = {
            "summary": summary,
            "input": {"compensation_reference": new_task["compensation_reference"]},
            "from": record["state"],
            "to": new_state,
            "task_id": task["id"],
            "breach_amount": new_task["breach_amount"],
        }
        result = self.repository.save_task_and_record(
            record["id"], expected_version, new_state, new_payload, task["id"], new_task,
            actor.user_id, "confirm_compensation", details,
        )
        return result["record"]

    def make_up_overview(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        """返回指令的补券任务与各批补券记录，页面据此显示缺口和状态。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        tasks = self.repository.list_tasks(record_id)
        active = next((t for t in reversed(tasks) if t["status"] in ("open", "breached")), None)
        overview: Dict[str, Any] = {"active_task": None, "tasks": []}
        for task in tasks:
            batches = self.repository.list_batches(task["id"])
            remaining = int(task["shortage_quantity"]) - int(task["delivered_quantity"])
            item = dict(task)
            item["remaining_quantity"] = remaining
            item["batches"] = batches
            overview["tasks"].append(item)
        if active is not None:
            active_item = next(item for item in overview["tasks"] if item["id"] == active["id"])
            overview["active_task"] = active_item
        return overview

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
