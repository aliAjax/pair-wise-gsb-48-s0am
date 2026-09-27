import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.rules import DEFAULT_PENALTY_RATE


CREATE_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'none', 'action_ratio': 1.0}
OFFICER = Actor("officer", "settlement_officer")
TRADER = Actor("trader-1", "trader")


def approve(service, reference="TRD-25001", data=None):
    record = service.create(TRADER, reference, data or CREATE_DATA)
    record = service.act(OFFICER, record["id"], record["version"], "approve", {})
    return record


def fail(service, record, due, seller="SELLER-A", shortage=None, reason="卖方券不足"):
    data = {"fail_reason": reason, "responsible_seller": seller, "due_date": due}
    if shortage is not None:
        data["shortage_quantity"] = shortage
    return service.act(OFFICER, record["id"], record["version"], "fail", data)


class MakeUpFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.today = date.today()
        self.service = build_service(str(Path(self.temp.name) / "test.db"), clock=lambda: self.today)

    def tearDown(self):
        self.temp.cleanup()

    def test_fail_creates_single_open_task_with_reason_and_fields(self):
        record = approve(self.service)
        due = (date.today() + timedelta(days=2)).isoformat()
        record = fail(self.service, record, due, shortage=400)
        self.assertEqual(record["state"], "make_up")
        self.assertEqual(record["payload"]["fail_reason"], "卖方券不足")
        overview = self.service.make_up_overview(OFFICER, record["id"])
        task = overview["active_task"]
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["responsible_seller"], "SELLER-A")
        self.assertEqual(task["required_quantity"], 1000)
        self.assertEqual(task["shortage_quantity"], 400)
        self.assertEqual(task["delivered_quantity"], 0)
        self.assertEqual(task["remaining_quantity"], 400)
        self.assertEqual(task["due_date"], due)
        # 同一指令只能有一条未关闭任务：再失败被状态机与任务唯一约束拦住
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "fail", {"fail_reason": "再次失败", "responsible_seller": "SELLER-A", "due_date": due})

    def test_partial_batches_then_full_makeup_resumes_settlement(self):
        record = approve(self.service)
        due = (date.today() + timedelta(days=3)).isoformat()
        record = fail(self.service, record, due, shortage=300)
        record = self.service.act(OFFICER, record["id"], record["version"], "make_up_delivery", {"delivered_quantity": 100})
        self.assertEqual(record["state"], "make_up")
        overview = self.service.make_up_overview(OFFICER, record["id"])
        self.assertEqual(overview["active_task"]["remaining_quantity"], 200)
        self.assertEqual(len(overview["active_task"]["batches"]), 1)
        record = self.service.act(OFFICER, record["id"], record["version"], "make_up_delivery", {"delivered_quantity": 150})
        self.assertEqual(overview["active_task"]["status"], "open")
        # 补券不能超过缺口
        with self.assertRaises(ValidationError):
            self.service.act(OFFICER, record["id"], record["version"], "make_up_delivery", {"delivered_quantity": 999})
        record = self.service.act(OFFICER, record["id"], record["version"], "make_up_delivery", {"delivered_quantity": 50})
        # 补足后原指令回到待交收，任务关闭，正常交收继续
        self.assertEqual(record["state"], "approved")
        overview = self.service.make_up_overview(OFFICER, record["id"])
        self.assertIsNone(overview["active_task"])
        self.assertEqual(overview["tasks"][-1]["status"], "fulfilled")
        record = self.service.act(OFFICER, record["id"], record["version"], "settle", {"delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertEqual(record["state"], "settled")
        actions = [e["action"] for e in self.service.timeline(OFFICER, record["id"])]
        self.assertIn("fail", actions)
        self.assertEqual(actions.count("make_up_delivery"), 3)
        self.assertIn("settle", actions)

    def test_breach_after_due_date_uses_unfilled_market_value(self):
        record = approve(self.service)
        due = (self.today + timedelta(days=2)).isoformat()
        record = fail(self.service, record, due, shortage=300)
        record = self.service.act(OFFICER, record["id"], record["version"], "make_up_delivery", {"delivered_quantity": 100})
        # 未到截止日不能认定违约
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "mark_breach", {})
        # 时间推进到截止日后第4天
        self.today = self.today + timedelta(days=6)
        record = self.service.act(OFFICER, record["id"], record["version"], "mark_breach", {})
        self.assertEqual(record["state"], "pending_compensation")
        # 未补200股 × 12.5 = 2500；违约4天 × 默认日费率0.0005
        self.assertEqual(record["payload"]["breach_remaining_quantity"], 200)
        self.assertEqual(record["payload"]["breach_market_value"], 2500.0)
        self.assertEqual(record["payload"]["breach_amount"], round(2500.0 * DEFAULT_PENALTY_RATE * 4, 2))
        overview = self.service.make_up_overview(OFFICER, record["id"])
        self.assertEqual(overview["active_task"]["status"], "breached")
        self.assertFalse(overview["active_task"]["compensation_confirmed"])
        # 停在待赔付：不能继续补券，也不能直接冲正
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "make_up_delivery", {"delivered_quantity": 1})
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "想跳过赔付"})

    def test_confirm_compensation_then_reverse(self):
        record = approve(self.service)
        due = self.today.isoformat()
        record = fail(self.service, record, due, shortage=100)
        self.today = self.today + timedelta(days=1)
        record = self.service.act(OFFICER, record["id"], record["version"], "mark_breach", {})
        # 缺赔付凭证不能确认
        with self.assertRaises(ValidationError):
            self.service.act(OFFICER, record["id"], record["version"], "confirm_compensation", {})
        record = self.service.act(OFFICER, record["id"], record["version"], "confirm_compensation", {"compensation_reference": "PAY-001"})
        self.assertEqual(record["state"], "pending_compensation")
        self.assertTrue(record["payload"]["compensation_confirmed"])
        # 重复确认被拒绝
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "confirm_compensation", {"compensation_reference": "PAY-002"})
        record = self.service.act(OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "赔付完成后冲正"})
        self.assertEqual(record["state"], "reversed")
        overview = self.service.make_up_overview(OFFICER, record["id"])
        self.assertIsNone(overview["active_task"])
        self.assertEqual(overview["tasks"][-1]["status"], "compensated")
        timeline = self.service.timeline(OFFICER, record["id"])
        summaries = " ".join(str(e["details"].get("summary", "")) for e in timeline)
        self.assertIn("卖方券不足", summaries)
        self.assertIn("违约金", summaries)
        self.assertIn("赔付", summaries)

    def test_permissions(self):
        record = approve(self.service)
        due = (date.today() + timedelta(days=1)).isoformat()
        with self.assertRaises(PermissionDenied):
            self.service.act(TRADER, record["id"], record["version"], "fail", {"fail_reason": "x", "responsible_seller": "S", "due_date": due})


if __name__ == "__main__":
    unittest.main()
