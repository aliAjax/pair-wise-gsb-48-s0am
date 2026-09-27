import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'none', 'action_ratio': 1.0}
OFFICER = Actor("officer", "settlement_officer")
FUTURE = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")
PAST = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


class FailureHandlingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _approved(self, reference="TRD-30001"):
        record = self.service.create(Actor("creator", "trader"), reference, CREATE_DATA)
        record = self.service.act(OFFICER, record["id"], record["version"], "approve", {})
        self.assertEqual(record["state"], "approved")
        return record

    def _failed(self, reference="TRD-30001", shortfall=1000, due=FUTURE):
        record = self._approved(reference)
        return self.service.act(
            OFFICER,
            record["id"],
            record["version"],
            "fail",
            {"fail_reason": "卖方券不足", "responsible_seller": "SELLER-A", "shortfall_quantity": shortfall, "due_date": due},
        )

    def test_fail_creates_single_open_task_with_gap(self):
        record = self._failed(shortfall=1000, due=FUTURE)
        self.assertEqual(record["state"], "failed")
        task = record["replenishment"]
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["responsible_seller"], "SELLER-A")
        self.assertEqual(task["required_quantity"], 1000)
        self.assertEqual(task["replenished_quantity"], 0)
        self.assertEqual(task["outstanding_quantity"], 1000)
        self.assertEqual(task["due_date"], FUTURE)

    def test_fail_requires_reason_seller_shortfall_due(self):
        record = self._approved()
        with self.assertRaises(ValidationError):
            self.service.act(OFFICER, record["id"], record["version"], "fail", {"fail_reason": "x"})
        with self.assertRaises(ValidationError):
            self.service.act(OFFICER, record["id"], record["version"], "fail",
                             {"fail_reason": "卖方券不足", "responsible_seller": "S", "shortfall_quantity": 0, "due_date": FUTURE})
        with self.assertRaises(ValidationError):
            self.service.act(OFFICER, record["id"], record["version"], "fail",
                             {"fail_reason": "卖方券不足", "responsible_seller": "S", "shortfall_quantity": 1000, "due_date": "bad-date"})

    def test_second_fail_does_not_create_another_open_task(self):
        record = self._failed()
        # 已失败的指令不能再次 fail（状态机拦截）
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "fail",
                             {"fail_reason": "再次失败", "responsible_seller": "SELLER-B", "shortfall_quantity": 100, "due_date": FUTURE})
        detail = self.service.replenishment_detail(OFFICER, record["id"])
        self.assertEqual(detail["responsible_seller"], "SELLER-A")
        self.assertEqual(len(detail["lots"]), 0)

    def test_partial_then_full_replenishment_resumes_settlement(self):
        record = self._failed(shortfall=1000)

        record = self.service.act(OFFICER, record["id"], record["version"], "replenish", {"quantity": 600, "note": "第一批补券"})
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["replenishment"]["status"], "open")
        self.assertEqual(record["replenishment"]["replenished_quantity"], 600)
        self.assertEqual(record["replenishment"]["outstanding_quantity"], 400)

        with self.assertRaises(ValidationError):
            self.service.act(OFFICER, record["id"], record["version"], "replenish", {"quantity": 401})

        record = self.service.act(OFFICER, record["id"], record["version"], "replenish", {"quantity": 400, "note": "第二批补券"})
        # 补够后原指令继续交收
        self.assertEqual(record["state"], "approved")
        self.assertEqual(record["replenishment"]["status"], "completed")
        self.assertEqual(record["replenishment"]["outstanding_quantity"], 0)

        record = self.service.act(OFFICER, record["id"], record["version"], "settle",
                                  {"delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertEqual(record["state"], "settled")

        detail = self.service.replenishment_detail(OFFICER, record["id"])
        self.assertEqual(len(detail["lots"]), 2)
        self.assertEqual([lot["quantity"] for lot in detail["lots"]], [600, 400])

    def test_cannot_escalate_before_due_date(self):
        record = self._failed(due=FUTURE)
        self.service.act(OFFICER, record["id"], record["version"], "replenish", {"quantity": 400})
        record = self.service.get_record(OFFICER, record["id"])
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "escalate_compensation", {})

    def test_past_due_breach_penalty_and_pending_compensation(self):
        record = self._failed(shortfall=1000, due=PAST)
        record = self.service.act(OFFICER, record["id"], record["version"], "replenish", {"quantity": 400})
        record = self.service.act(OFFICER, record["id"], record["version"], "escalate_compensation", {})
        # 停在待赔付
        self.assertEqual(record["state"], "compensation_pending")
        self.assertEqual(record["replenishment"]["status"], "breached")
        # 未补 600 股 × 12.5
        self.assertEqual(record["payload"]["penalty_amount"], 7500.0)
        self.assertEqual(record["payload"]["outstanding_quantity"], 600)
        self.assertEqual(record["replenishment"]["penalty_amount"], 7500.0)

        # 未确认赔付不能冲正
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "误冲正"})

        # 赔付不足不允许确认
        with self.assertRaises(ValidationError):
            self.service.act(OFFICER, record["id"], record["version"], "confirm_compensation", {"compensation_amount": 1.0})

        record = self.service.act(OFFICER, record["id"], record["version"], "confirm_compensation", {})
        self.assertTrue(record["payload"]["compensation_confirmed"])
        self.assertEqual(record["replenishment"]["status"], "compensated")

        # 确认赔付后才能冲正
        record = self.service.act(OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "违约了结"})
        self.assertEqual(record["state"], "reversed")

    def test_audit_timeline_covers_handling(self):
        record = self._failed(shortfall=1000, due=PAST)
        self.service.act(OFFICER, record["id"], record["version"], "replenish", {"quantity": 1000})
        record = self.service.get_record(OFFICER, record["id"])
        actions = [event["action"] for event in self.service.timeline(OFFICER, record["id"])]
        self.assertEqual(
            actions,
            ["created", "approve", "fail", "replenish_task_created", "replenish", "replenish_completed"],
        )

    def test_replenish_requires_open_task(self):
        record = self._approved()
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "replenish", {"quantity": 10})

    def test_breach_then_full_replenishment_is_blocked(self):
        record = self._failed(shortfall=1000, due=PAST)
        record = self.service.act(OFFICER, record["id"], record["version"], "escalate_compensation", {})
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "replenish", {"quantity": 600})

    def test_only_settlement_officer_can_handle_failure(self):
        record = self._approved()
        with self.assertRaises(Exception):
            self.service.act(Actor("ca", "corporate_actions"), record["id"], record["version"], "fail",
                             {"fail_reason": "卖方券不足", "responsible_seller": "S", "shortfall_quantity": 1, "due_date": FUTURE})


if __name__ == "__main__":
    unittest.main()
