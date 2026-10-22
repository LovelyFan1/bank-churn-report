"""会话绑定的提议、确认、清空及并发；仅使用独立 SQLite fixture。"""
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.agent import context, order_safety
from app.migrate import run_migrations
from app.models.work_order import WorkOrder
import test_agent_order_safety as fixtures


class AgentSessionRepairTests(unittest.TestCase):
    setUp = fixtures.OrderSafetyTests.setUp
    tearDown = fixtures.OrderSafetyTests.tearDown

    def seed_session(self, sid="repair", *, username="tester", version=0, ctx=None):
        with self.sessions() as db:
            db.add(order_safety.AgentSession(
                username=username, session_id=sid,
                context=json.dumps(ctx or context.empty()), version=version,
                expires_at=time.time() + 3600, busy_until=0))
            db.commit()

    def state(self, sid="repair"):
        return self.client.get(f"/api/agent/session/{sid}").json()

    def prepare(self, ids=None, *, sid="repair", version=0):
        response = self.client.post("/api/agent/prepare-create", json={
            "customer_ids": ids or ["C000001"], "assignee": "tester",
            "session_id": sid, "session_version": version})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def proposal(item):
        return {"action": "create_work_order", "payload": {
            k: v for k, v in item.items() if k != "confirmation_token"},
            "confirmation_token": item["confirmation_token"]}

    def confirm(self, proposal, *, sid="repair", version=0, mode="single"):
        return self.client.post("/api/agent/confirm", json={
            **proposal, "session_id": sid, "session_version": version,
            "context_mode": mode})

    def test_confirmed_create_updates_authoritative_focus_for_next_pronoun(self):
        old = context.empty()
        old.update(focus_customer_id="C071081", orders=[{
            "order_id": 56, "customer_id": "C071081", "status": "in_progress"}])
        self.seed_session(version=1, ctx=old)
        self.assertEqual(self.state()["context"]["focus_customer_id"], "C071081")
        prepared = self.prepare(version=1)
        self.assertEqual(self.state()["context"]["focus_customer_id"], "C071081")
        response = self.confirm(self.proposal(prepared["items"][0]),
                                version=prepared.get("session_version", 0))
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        self.assertEqual(receipt.get("context", {}).get("focus_customer_id"), "C000001")
        persisted = self.state()
        self.assertEqual(persisted["context"], receipt["context"])
        self.assertEqual(persisted["session_version"], receipt["session_version"])
        followup = self.client.post("/api/agent/ask", json={
            "question": "取消该工单", "session_id": "repair",
            "session_version": receipt["session_version"]})
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(followup.json()["pending_action"]["payload"]["order_id"], receipt["order"]["id"])

    def test_clear_revokes_unlisted_session_tokens_but_preserves_other_scopes(self):
        self.seed_session("first")
        self.seed_session("second")
        first = self.prepare(sid="first")
        second = self.prepare(ids=["C000002"], sid="second")
        with self.sessions() as db:
            db.add(order_safety.PendingAgentAction(token_hash="other-user", username="other",
                action="create_work_order", payload="{}", session_id="first",
                expires_at=time.time() + 600, consumed=0))
            db.add(order_safety.PendingAgentAction(token_hash="legacy", username="tester",
                action="create_work_order", payload="{}", session_id=None,
                expires_at=time.time() + 600, consumed=0))
            db.commit()
        cleared = self.client.post("/api/agent/session/first/clear", json={"confirmation_tokens": []})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(self.confirm(self.proposal(first["items"][0]), sid="first",
            version=first["session_version"]).status_code, 409)
        self.assertEqual(self.confirm(self.proposal(second["items"][0]), sid="second",
            version=second["session_version"]).status_code, 200)
        with self.sessions() as db:
            self.assertEqual(db.get(order_safety.PendingAgentAction, "other-user").consumed, 0)
            self.assertEqual(db.get(order_safety.PendingAgentAction, "legacy").consumed, 0)

    def test_bound_token_requires_matching_session_and_current_version(self):
        self.seed_session()
        self.seed_session("other")
        prepared = self.prepare()
        proposal = self.proposal(prepared["items"][0])
        self.assertEqual(self.client.post("/api/agent/confirm", json=proposal).status_code, 409)
        self.assertEqual(self.confirm(proposal, sid="other").status_code, 409)
        self.assertEqual(self.confirm(proposal, version=prepared["session_version"] + 1).status_code, 409)
        response = self.confirm(proposal, version=prepared["session_version"])
        self.assertEqual(response.status_code, 200, response.text)

    def test_unbound_legacy_token_is_not_adopted_by_a_new_session(self):
        self.seed_session()
        prepared = self.client.post("/api/agent/prepare-create", json={
            "customer_ids": ["C000001"], "assignee": "tester"}).json()
        proposal = self.proposal(prepared["items"][0])
        self.assertEqual(self.confirm(proposal).status_code, 409)
        response = self.client.post("/api/agent/confirm", json=proposal)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.state()["session_version"], 0)

    def test_stale_prepare_cannot_issue_more_tokens(self):
        self.seed_session()
        prepared = self.prepare()
        response = self.client.post("/api/agent/prepare-create", json={
            "customer_ids": ["C000002"], "assignee": "tester",
            "session_id": "repair", "session_version": 0})
        self.assertEqual(response.status_code, 409, response.text)
        with self.sessions() as db:
            self.assertEqual(db.query(order_safety.PendingAgentAction).count(), 1)
        self.assertEqual(self.state()["session_version"], prepared["session_version"])

    def test_confirm_refuses_busy_session_and_rolls_back_token_claim(self):
        self.seed_session()
        prepared = self.prepare()
        proposal = self.proposal(prepared["items"][0])
        with self.sessions() as db:
            db.get(order_safety.AgentSession, ("tester", "repair")).busy_until = time.time() + 30
            db.commit()
        self.assertEqual(self.confirm(proposal, version=prepared["session_version"]).status_code, 409)
        with self.sessions() as db:
            self.assertEqual(db.query(order_safety.PendingAgentAction).one().consumed, 0)

    def test_business_and_session_update_roll_back_together(self):
        self.seed_session()
        prepared = self.prepare()
        before = self.state()
        proposal = self.proposal(prepared["items"][0])
        with patch("app.services.auth_deps.write_audit", side_effect=RuntimeError("fixture audit failure")):
            response = self.confirm(proposal, version=prepared["session_version"])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.state()["context"], before["context"])
        self.assertEqual(self.state()["session_version"], before["session_version"])
        with self.sessions() as db:
            self.assertEqual(db.query(WorkOrder).count(), 3)
            self.assertEqual(db.query(order_safety.PendingAgentAction).one().consumed, 0)
        self.assertEqual(self.confirm(proposal, version=prepared["session_version"]).status_code, 200)

    def test_batch_confirmation_advances_versions_and_keeps_all_orders_without_focus(self):
        old = context.empty()
        old.update(focus_customer_id="C071081", orders=[{
            "order_id": 56, "customer_id": "C071081", "status": "in_progress"}])
        self.seed_session(version=1, ctx=old)
        prepared = self.prepare(["C000001", "C000002"], version=1)
        response = self.client.post("/api/agent/confirm-batch", json={
            "proposals": [self.proposal(it) for it in prepared["items"]],
            "session_id": "repair", "session_version": prepared.get("session_version", 0)})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["succeeded"], 2)
        self.assertIsNone(result.get("context", {}).get("focus_customer_id"))
        self.assertEqual({it["customer_id"] for it in result.get("context", {}).get("orders", [])},
                         {"C000001", "C000002"})
        self.assertEqual(self.state()["context"], result["context"])

    def test_one_customer_card_batch_confirms_with_a_single_new_focus(self):
        old = context.empty()
        old.update(focus_customer_id="C071081", orders=[{
            "order_id": 56, "customer_id": "C071081", "status": "in_progress"}])
        self.seed_session(version=1, ctx=old)
        prepared = self.prepare(version=1)
        response = self.client.post("/api/agent/confirm-batch", json={
            "proposals": [self.proposal(prepared["items"][0])],
            "session_id": "repair", "session_version": prepared["session_version"]})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["context"]["focus_customer_id"], "C000001")
        self.assertEqual([it["customer_id"] for it in result["context"]["orders"]], ["C000001"])
        followup = self.client.post("/api/agent/ask", json={
            "question": "取消该工单", "session_id": "repair",
            "session_version": result["session_version"]})
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(followup.json()["pending_action"]["payload"]["order_id"], result["created"][0]["id"])

    def test_all_failed_batch_after_clear_returns_conflict_for_client_refresh(self):
        self.seed_session(version=1)
        prepared = self.prepare(["C000001", "C000002"], version=1)
        self.client.post("/api/agent/session/repair/clear", json={"confirmation_tokens": []})
        response = self.client.post("/api/agent/confirm-batch", json={
            "proposals": [self.proposal(it) for it in prepared["items"]],
            "session_id": "repair", "session_version": prepared["session_version"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(self.state()["expired"])
        with self.sessions() as db:
            self.assertEqual(db.query(WorkOrder).count(), 3)

    def test_cancel_preparation_returns_bound_authoritative_context(self):
        self.seed_session()
        prepared = self.client.post("/api/agent/prepare-order-cancel", json={
            "order_id": 56, "customer_id": "C071081", "session_id": "repair", "session_version": 0})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        pending = prepared.json()
        self.assertEqual(pending["context"], self.state()["context"])
        confirmed = self.confirm(pending, version=pending["session_version"])
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["context"]["orders"][0]["status"], "cancelled")

    def test_expired_lease_cannot_publish_or_issue_into_a_newer_request(self):
        from app.routers import agent
        from fastapi import HTTPException
        self.seed_session()
        started = time.time()
        with self.sessions() as original, self.sessions() as newer:
            with patch.object(agent.time, "time", return_value=started):
                agent._start_session(original, self.user, agent.AskRequest(
                    question="查询", session_id="repair", session_version=0))
            with patch.object(agent.time, "time", return_value=started + 200):
                agent._start_session(newer, self.user, agent.AskRequest(
                    question="查询", session_id="repair", session_version=0))
                changed = context.empty()
                changed["focus_customer_id"] = "C000002"
                agent._finish_session(newer, self.user, "repair", changed)
                with self.assertRaises(HTTPException) as invalid:
                    order_safety.issue(original, self.user, {
                        "action": "create_work_order", "payload": {"customer_id": "C000001"}},
                        session_id="repair")
                self.assertEqual(invalid.exception.status_code, 409)
                original.rollback()
                with self.assertRaises(HTTPException) as stale:
                    agent._finish_session(original, self.user, "repair", context.empty())
                self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(self.state()["context"]["focus_customer_id"], "C000002")

    def test_two_confirmations_at_same_version_do_not_overwrite_context(self):
        self.seed_session()
        prepared = self.prepare(["C000001", "C000002"])
        proposals = [self.proposal(it) for it in prepared["items"]]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda p: self.confirm(p, version=prepared.get("session_version", 0)), proposals))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        winner = next(r.json() for r in responses if r.status_code == 200)
        self.assertEqual(self.state()["context"], winner["context"])

    def test_ask_proposals_are_bound_and_clear_does_not_need_browser_token_list(self):
        answer = self.client.post("/api/agent/ask", json={
            "question": "取消C071081工单", "session_id": "repair", "session_version": 0})
        self.assertEqual(answer.status_code, 200, answer.text)
        result = answer.json()
        pending = result["pending_action"]
        with self.sessions() as db:
            self.assertEqual(db.query(order_safety.PendingAgentAction).one().session_id, "repair")
        self.client.post("/api/agent/session/repair/clear", json={"confirmation_tokens": []})
        self.assertEqual(self.confirm(pending, version=result["session_version"]).status_code, 409)

    def test_delete_clears_old_object_scope(self):
        current = context.empty()
        current.update(focus_customer_id="C071081", customers=[{"id": "C000001"}],
            orders=[{"order_id": 8, "customer_id": "C062858"},
                    {"order_id": 56, "customer_id": "C071081"}])
        self.seed_session(ctx=current)
        with self.sessions() as db:
            proposal = order_safety.proposal(db.get(WorkOrder, 56), delete=True)
            order_safety.issue(db, self.user, proposal, session_id="repair")
        response = self.confirm(proposal)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["context"]["orders"], [])
        self.assertFalse(response.json()["context"].get("focus_customer_id"))
        with self.assertRaises(ValueError):
            order_safety.target("取消该工单", response.json()["context"])

    def test_migration_is_additive_and_rejects_new_invalid_statuses(self):
        run_migrations(self.engine)
        self.assertEqual(run_migrations(self.engine), [])
        with self.engine.begin() as conn:
            with self.assertRaises(IntegrityError):
                conn.execute(text("UPDATE work_orders SET status=NULL WHERE id=56"))
            with self.assertRaises(IntegrityError):
                conn.execute(text("INSERT INTO work_orders (customer_id,status) VALUES ('fixture','invalid')"))

    def test_first_binding_migration_revokes_old_tokens_once_without_deleting_them(self):
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE pending_agent_actions"))
            conn.execute(text("""CREATE TABLE pending_agent_actions (
                token_hash VARCHAR(64) PRIMARY KEY, username VARCHAR NOT NULL,
                action VARCHAR NOT NULL, payload TEXT NOT NULL, snapshot TEXT,
                expires_at FLOAT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)"""))
            conn.execute(text("""INSERT INTO pending_agent_actions
                (token_hash,username,action,payload,expires_at,consumed)
                VALUES ('before','tester','create_work_order','unchanged',99999999999,0)"""))
        applied = run_migrations(self.engine)
        self.assertIn("pending_agent_actions.session_id", applied)
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT payload,session_id,consumed FROM pending_agent_actions WHERE token_hash='before'")).one()
            self.assertEqual(tuple(row), ("unchanged", None, 1))
            conn.execute(text("""INSERT INTO pending_agent_actions
                (token_hash,username,action,payload,expires_at,consumed)
                VALUES ('after','tester','create_work_order','new legacy request',99999999999,0)"""))
        self.assertEqual(run_migrations(self.engine), [])
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT consumed FROM pending_agent_actions WHERE token_hash='after'")).scalar(), 0)

    def test_status_migration_preserves_existing_invalid_history(self):
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE work_orders"))
            conn.execute(text("CREATE TABLE work_orders (id INTEGER PRIMARY KEY, customer_id VARCHAR, status VARCHAR, note TEXT)"))
            conn.execute(text("INSERT INTO work_orders (id,customer_id,status,note) VALUES (1,'old',NULL,'historical')"))
        run_migrations(self.engine)
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT status,note FROM work_orders WHERE id=1")).one()
            self.assertEqual(tuple(row), (None, "historical"))
            conn.execute(text("UPDATE work_orders SET note='annotated' WHERE id=1"))
            with self.assertRaises(IntegrityError):
                conn.execute(text("UPDATE work_orders SET status=NULL WHERE id=1"))


if __name__ == "__main__":
    unittest.main()
