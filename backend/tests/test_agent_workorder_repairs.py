"""Agent result entry and work-order status invariants, using isolated SQLite."""
import unittest
from unittest.mock import patch

import test_agent_order_safety as fixtures
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.agent import order_safety, tools
from app.models.work_order import WorkOrder


class WorkOrderRepairsTests(unittest.TestCase):
    setUp = fixtures.OrderSafetyTests.setUp
    tearDown = fixtures.OrderSafetyTests.tearDown

    def detail(self):
        return {"customer_id": "C071081", "customer_name": "Bentley",
                "status": "in_progress", "assignee": "tester"}

    def test_explicit_null_status_is_rejected_without_changing_order(self):
        response = self.client.put("/api/work-orders/56", json={"status": None})
        self.assertEqual(response.status_code, 422, response.text)
        with self.sessions() as db:
            self.assertEqual(db.get(WorkOrder, 56).status, "in_progress")

    def test_omitted_status_allows_note_update(self):
        response = self.client.put("/api/work-orders/56", json={"note": "客户约定次日回访"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "in_progress")
        self.assertEqual(response.json()["note"], "客户约定次日回访")

    def test_null_status_cannot_release_customer_active_order_lock(self):
        self.client.put("/api/work-orders/56", json={"status": None})
        response = self.client.post("/api/agent/prepare-create", json={
            "customer_ids": ["C071081"], "assignee": "tester"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(len(response.json()["failed"]), 1)

    def test_new_database_rejects_null_and_invalid_status_outside_api(self):
        for status in (None, "unknown"):
            with self.subTest(status=status), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    connection.execute(text("UPDATE work_orders SET status=:status WHERE id=56"),
                                       {"status": status})

    def test_agent_completed_requires_an_explicit_outcome(self):
        with patch.object(tools, "_get", return_value=self.detail()):
            with self.assertRaisesRegex(ValueError, "结果"):
                tools.propose_update_work_order(56, status="completed", customer_id="C071081")

    def test_agent_accepts_each_explicit_completed_outcome_and_displays_it(self):
        for outcome, label in (("retained", "挽留成功"), ("lost", "流失")):
            with self.subTest(outcome=outcome), patch.object(tools, "_get", return_value=self.detail()):
                pending = tools.propose_update_work_order(
                    56, status="completed", customer_id="C071081", result=outcome)
                self.assertEqual(pending["payload"]["result"], outcome)
                self.assertIn(label, pending["summary"])

    def test_agent_rejects_invalid_or_inconsistent_outcomes_before_proposal(self):
        with patch.object(tools, "_get", return_value=self.detail()):
            for status, outcome in (("completed", "unknown"), ("pending", "retained"),
                                    ("lost", "retained")):
                with self.subTest(status=status, outcome=outcome), self.assertRaises(ValueError):
                    tools.propose_update_work_order(
                        56, status=status, customer_id="C071081", result=outcome)

    def test_agent_completed_confirmation_keeps_explicit_result(self):
        with patch.object(tools, "_get", return_value=self.detail()):
            pending = tools.propose_update_work_order(
                56, status="completed", customer_id="C071081", result="retained")
        with self.sessions() as db:
            order_safety.issue(db, self.user, pending)
        response = self.client.post("/api/agent/confirm", json={
            "action": pending["action"], "payload": pending["payload"],
            "confirmation_token": pending["confirmation_token"]})
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            order = db.get(WorkOrder, 56)
            self.assertEqual((order.status, order.result), ("completed", "retained"))
            self.assertIsNotNone(order.completed_at)


if __name__ == "__main__":
    unittest.main()
