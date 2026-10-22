"""隔离 SQLite 回归：绝不请求运行中的服务或使用业务数据库。

运行：DATABASE_URL=sqlite:// python -m unittest discover -s tests -v
"""
import copy
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.models.user import User, AuditLog
from app.models.work_order import WorkOrder
from app.models.customer import Customer
from app.agent import context, graph, tools, order_safety
from app.routers import agent, work_orders, customers, models, eda, cost_benefit, clustering, tasks, portfolio, auth as auth_router
from app.services import customer_service, risk_scoring
from app.services import auth_deps


class OrderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'test.db'}",
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.user = User(username="tester", display_name="测试经理", role="manager",
                         password_hash="not-a-login", is_active=1)
        with self.sessions() as db:
            db.add(self.user)
            db.add_all([Customer(customer_id="C000001", surname="Test", geography="France", exited=0),
                        Customer(customer_id="C000002", surname="Second", geography="France", exited=0),
                        Customer(customer_id="C000003", surname="Exited", geography="France", exited=1),
                        Customer(customer_id="C071081", surname="Bentley", geography="France", exited=0)])
            db.add_all([
                WorkOrder(id=8, customer_id="C062858", customer_name="Pirogov",
                          status="completed", result="retained", assignee="tester"),
                WorkOrder(id=25, customer_id="C061493", customer_name="Tochukwu",
                          status="completed", result="retained", assignee="tester"),
                WorkOrder(id=56, customer_id="C071081", customer_name="Bentley",
                          status="in_progress", assignee="tester"),
            ])
            db.commit()
        app = FastAPI()
        from fastapi.exceptions import RequestValidationError
        from app.validation import validation_error_handler
        app.add_exception_handler(RequestValidationError, validation_error_handler)
        app.include_router(agent.router)
        app.include_router(work_orders.router)
        for module in (customers, models, eda, cost_benefit, clustering, tasks, portfolio, auth_router):
            app.include_router(module.router)
        def db_dep():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = db_dep
        app.dependency_overrides[auth_deps.current_user] = lambda: self.user
        self.client = TestClient(app)
        def detail(db, cid):
            row = db.query(Customer).filter(Customer.customer_id == cid).first()
            if not row:
                return None
            return {"customer_id": cid, "surname": row.surname, "geography": "France",
                    "exited": row.exited, "probability": 0.8, "balance": 50000.0,
                    "risk_level": "HIGH", "value_tier": "LOW", "expected_value": 40000.0,
                    "channel": "outbound", "action": "联系客户", "risk_factors": [],
                    "has_active_order": bool(db.query(WorkOrder.id).filter(
                        WorkOrder.customer_id == cid, WorkOrder.status.in_(("pending", "in_progress"))).first())}
        self.patches = [patch("app.database.SessionLocal", self.sessions),
                        patch.object(agent.llm, "is_available", return_value=(True, "")),
                        patch.object(customer_service, "get_customer_detail", side_effect=detail),
                        patch.object(risk_scoring, "get_risk_info", return_value={
                            "model": "test", "thresholds": {"critical": .7, "high": .3, "medium": .1}}),
                        patch.object(risk_scoring, "_read_model_version", return_value="test-version")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def ask(self, question="为C071081取消工单", ctx=None):
        r = self.client.post("/api/agent/ask", json={"question": question, "context": ctx})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def confirm(self, p):
        return self.client.post("/api/agent/confirm", json={
            "action": p["action"], "payload": p["payload"],
            "confirmation_token": p.get("confirmation_token"),
            **{key: p[key] for key in ("session_id", "session_version") if key in p}})

    def test_chinese_adjacent_ids(self):
        self.assertEqual(context.explicit_ids("为C071081取消工单"), ["C071081"])
        self.assertEqual(context.explicit_ids("查c071081的工单"), ["c071081"])
        self.assertEqual(context.explicit_ids("XC0710812a"), [])

    def test_cancel_exact_customer_keeps_other_orders_and_record(self):
        p = self.ask()["pending_action"]
        self.assertEqual(p["payload"]["order_id"], 56)
        self.assertEqual(p["action"], "update_work_order")
        with self.sessions() as db:
            self.assertEqual(db.get(WorkOrder, 56).status, "in_progress")
        r = self.confirm(p)
        self.assertEqual(r.status_code, 200, r.text)
        with self.sessions() as db:
            self.assertEqual(db.get(WorkOrder, 56).status, "cancelled")
            self.assertIsNone(db.get(WorkOrder, 56).result)
            self.assertEqual(db.get(WorkOrder, 8).status, "completed")
            self.assertEqual(db.get(WorkOrder, 25).status, "completed")
            self.assertEqual(db.query(WorkOrder).count(), 3)
            self.assertTrue(db.query(AuditLog).filter_by(source="agent", success=1).first())
        stats = self.client.get("/api/work-orders/stats").json()
        self.assertEqual(stats["cancelled"], 1)
        self.assertEqual(stats["total"], 3)

    def test_focus_overrides_unrelated_history(self):
        old = {"customers": [{"id": "C062858"}], "orders": [
            {"order_id": 25, "customer_id": "C061493", "status": "completed"}]}
        cur = context.extract([{"tool": "get_customer_risk", "ok": True,
                                "data": {"customer_id": "C071081", "surname": "Bentley"}}], None)
        ctx = context.merge(old, cur)
        self.assertEqual(ctx["orders"], [])
        p = self.ask("取消该工单", ctx)["pending_action"]
        self.assertEqual(p["payload"]["order_id"], 56)

    def test_multiple_context_candidates_require_clarification(self):
        result = self.ask("取消该工单", {"customers": [
            {"id": "C071081"}, {"id": "C062858"}], "orders": []})
        self.assertIsNone(result["pending_action"])
        self.assertIn("不唯一", result["text"])

    def test_multiple_active_orders_require_explicit_order(self):
        from sqlalchemy import text
        with self.sessions() as db:
            # 模拟迁移前已存在的脏数据，验证解析器仍不会任意挑选。
            db.execute(text("DROP INDEX uq_active_customer_work_order"))
            db.add(WorkOrder(id=57, customer_id="C071081", status="pending"))
            db.commit()
        self.assertIsNone(self.ask()["pending_action"])
        self.assertEqual(self.ask("取消C071081工单#56")["pending_action"]["payload"]["order_id"], 56)

    def test_missing_or_completed_cannot_cancel(self):
        for q in ("取消C999999工单", "取消C062858工单", "取消工单#8", "取消C071081工单#8"):
            with self.subTest(q=q):
                self.assertIsNone(self.ask(q)["pending_action"])

    def test_tools_reject_customer_mismatch(self):
        with patch.object(tools, "_get", return_value={"customer_id": "C062858", "status": "completed"}):
            for fn in (tools.propose_update_work_order, tools.propose_delete_work_order):
                with self.assertRaises(ValueError):
                    fn(order_id=8, customer_id="C071081")

    def test_graph_scope_does_not_authorize_every_queried_order(self):
        state = {"question": "为C071081取消工单", "context_in": {}, "tool_results": []}
        result = [{"tool": "list_work_orders", "ok": True, "data": {"items": [
            {"order_id": 8, "customer_id": "C062858"}]}}]
        self.assertIsNotNone(graph._scope_error(state, "propose_delete_work_order", {"order_id": 8}, result))
        self.assertIsNotNone(graph._scope_error(state, "propose_update_work_order",
            {"order_id": 8, "customer_id": "C062858", "status": "cancelled"}, result))

    def test_filtered_query_before_pagination(self):
        r = self.client.get("/api/work-orders", params={"customer_id": "C071081", "page_size": 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(r.json()["items"][0]["id"], 56)
        self.assertEqual(self.client.get("/api/work-orders", params={"status": "bogus"}).status_code, 422)
        with patch.object(tools, "_get") as get:
            with self.assertRaises(ValueError):
                tools.list_work_orders(status="bogus")
            get.assert_not_called()

    def test_missing_token_rejected(self):
        self.assertEqual(self.client.post("/api/agent/confirm", json={
            "action": "delete_work_order", "payload": {"order_id": 8}}).status_code, 409)

    def test_payload_tamper_and_replay_rejected(self):
        p = self.ask()["pending_action"]
        bad = copy.deepcopy(p)
        bad["payload"]["order_id"] = 8
        self.assertEqual(self.confirm(bad).status_code, 409)
        self.assertEqual(self.confirm(p).status_code, 200)
        self.assertEqual(self.confirm(p).status_code, 409)

    def test_action_tamper_rejected(self):
        p = self.ask()["pending_action"]
        p["action"] = "delete_work_order"
        self.assertEqual(self.confirm(p).status_code, 409)

    def test_expired_and_cross_user_token_rejected(self):
        p = self.ask()["pending_action"]
        self.user = User(username="another", display_name="另一经理", role="manager", is_active=1)
        self.assertEqual(self.confirm(p).status_code, 409)
        self.user.username = "tester"
        with self.sessions() as db:
            db.query(order_safety.PendingAgentAction).update({"expires_at": time.time() - 1})
            db.commit()
        self.assertEqual(self.confirm(p).status_code, 409)

    def test_permission_revoked_at_confirmation(self):
        p = self.ask()["pending_action"]
        self.user.role = "viewer"
        self.assertEqual(self.confirm(p).status_code, 403)

    def test_stale_snapshot_rejected(self):
        p = self.ask()["pending_action"]
        with self.sessions() as db:
            db.get(WorkOrder, 56).note = "另一位经理修改过备注"
            db.commit()
        self.assertEqual(self.confirm(p).status_code, 409)

    def test_explicit_delete_only(self):
        p = self.ask("永久删除C062858工单#8")["pending_action"]
        self.assertEqual(p["action"], "delete_work_order")
        self.assertEqual(self.confirm(p).status_code, 200)
        with self.sessions() as db:
            self.assertIsNone(db.get(WorkOrder, 8))
            self.assertIsNotNone(db.get(WorkOrder, 56))

    def test_list_cancel_prepare_checks_ownership(self):
        r = self.client.post("/api/agent/prepare-order-cancel", json={
            "order_id": 8, "customer_id": "C071081"})
        self.assertEqual(r.status_code, 409)
        r = self.client.post("/api/agent/prepare-order-cancel", json={
            "order_id": 56, "customer_id": "C071081"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.confirm(r.json()).status_code, 200)

    def test_api_rejects_cancel_completed_and_reopen_duplicate(self):
        r = self.client.put("/api/work-orders/8", json={"status": "cancelled"})
        self.assertEqual(r.status_code, 409)
        with self.sessions() as db:
            db.add(WorkOrder(id=57, customer_id="C071081", status="cancelled"))
            db.commit()
        r = self.client.put("/api/work-orders/57", json={"status": "in_progress"})
        self.assertEqual(r.status_code, 409)

    def test_atomic_rollback_if_audit_fails(self):
        p = self.ask()["pending_action"]
        original = auth_deps.write_audit
        def audit(*args, **kwargs):
            if kwargs.get("source") == "agent":
                raise RuntimeError("simulated audit failure")
            return original(*args, **kwargs)
        with patch.object(auth_deps, "write_audit", side_effect=audit):
            self.assertEqual(self.confirm(p).status_code, 500)
        with self.sessions() as db:
            self.assertEqual(db.get(WorkOrder, 56).status, "in_progress")
            self.assertEqual(db.query(order_safety.PendingAgentAction).first().consumed, 0)
        self.assertEqual(self.confirm(p).status_code, 200)

    def test_consultation_not_treated_as_cancel_command(self):
        for question in ("怎么取消工单", "查看已取消工单", "不要取消工单#56", "取消筛选"):
            self.assertIsNone(order_safety.operation(question))

    def test_concurrent_confirm_only_one_executes(self):
        p = self.ask()["pending_action"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = sorted(pool.map(lambda _: self.confirm(p).status_code, range(2)))
        self.assertEqual(codes, [200, 409])
        with self.sessions() as db:
            self.assertEqual(db.query(AuditLog).filter_by(source="agent", success=1).count(), 1)

    def test_query_list_is_narrowed_to_explicit_customer(self):
        from langchain_core.messages import AIMessage
        state = {"question": "查询C071081的工单", "context_in": {}, "messages": [
            AIMessage(content="", tool_calls=[{"name": "list_work_orders",
                       "args": {}, "id": "test"}])], "tool_results": []}
        with patch.object(tools, "call_tool", return_value={"ok": True, "result": {"items": []}}) as call:
            graph.node_tools(state)
        self.assertEqual(call.call_args.args[1]["customer_id"], "C071081")

    def test_model_wrong_update_proposal_is_rejected_at_router(self):
        with self.sessions() as db:
            p = order_safety.proposal(db.get(WorkOrder, 8))
            p["payload"]["status"] = "in_progress"
        with patch.object(graph, "run", return_value={"pending_action": p}):
            result = self.ask("把C071081的工单改为处理中")
        self.assertIsNone(result["pending_action"])
        with self.sessions() as db:
            self.assertEqual(db.query(order_safety.PendingAgentAction).count(), 0)

    def test_normal_update_and_snapshot_change_before_issuing(self):
        with self.sessions() as db:
            p = order_safety.proposal(db.get(WorkOrder, 56))
            p["payload"] = {"order_id": 56, "customer_id": "C071081", "note": "正常更新备注"}
        with patch.object(graph, "run", return_value={"pending_action": p}):
            issued = self.ask("修改C071081工单#56的备注")["pending_action"]
        self.assertEqual(self.confirm(issued).status_code, 200)
        with self.sessions() as db:
            self.assertEqual(db.get(WorkOrder, 56).note, "正常更新备注")
            p["current"]["status"] = "completed"
            with self.assertRaises(Exception) as caught:
                order_safety.issue(db, self.user, p)
            self.assertEqual(caught.exception.status_code, 409)

    def test_new_customer_list_clears_focus(self):
        old = {"focus_customer_id": "C071081", "orders": [{"order_id": 56}], "customers": []}
        cur = context.extract([{"tool": "list_customers", "ok": True,
                                "data": {"items": [{"customer_id": "C062858"}]}}], None)
        result = context.merge(old, cur)
        self.assertIsNone(result["focus_customer_id"])
        self.assertEqual(result["orders"], [])

    def test_batch_command_does_not_silently_select_one(self):
        self.assertIsNone(self.ask("取消这些工单", {"focus_customer_id": "C071081"})["pending_action"])

    def test_failed_explicit_lookup_never_falls_back_to_old_customer(self):
        first = self.ask("取消C999999工单", {"focus_customer_id": "C071081"})
        self.assertEqual(first["context"]["focus_customer_id"], "C999999")
        self.assertIsNone(self.ask("取消该工单", first["context"])["pending_action"])

    def test_single_create_still_works_with_confirmation(self):
        p = {"action": "create_work_order", "payload": {
            "customer_id": "C000001", "customer_name": "Test", "assignee": "tester",
            "thresholds_snapshot": {}, "model_used": "test"}}
        with patch.object(graph, "run", return_value={"pending_action": p}):
            issued = self.ask("给C000001建单")["pending_action"]
        r = self.confirm(issued)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["order"]["customer_id"], "C000001")
        self.assertEqual(self.confirm(issued).status_code, 409)

    def test_cancelled_order_can_reopen_without_another_active_order(self):
        self.confirm(self.ask()["pending_action"])
        r = self.client.put("/api/work-orders/56", json={"status": "in_progress"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "in_progress")
        self.assertIsNone(r.json()["result"])


class ProjectRepairTests(unittest.TestCase):
    setUp = OrderSafetyTests.setUp
    tearDown = OrderSafetyTests.tearDown
    confirm = OrderSafetyTests.confirm

    def prepare(self, ids=None, assignee="tester"):
        response = self.client.post("/api/agent/prepare-create", json={
            "customer_ids": ids or ["C000001"], "assignee": assignee})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def proposal(item):
        return {"action": "create_work_order", "payload": {
            k: v for k, v in item.items() if k != "confirmation_token"},
            "confirmation_token": item["confirmation_token"]}

    def test_staff_cannot_bypass_insight_permissions(self):
        self.user.role = "staff"
        for path in ("/api/customers", "/api/customers/C000001", "/api/customers/export",
                     "/api/model/comparison", "/api/eda/comprehensive",
                     "/api/cost-benefit/summary", "/api/portfolio/matrix", "/api/cluster/profiles", "/api/tasks/x"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)

    def test_portfolio_matrix_role_permissions(self):
        from app.config import settings
        expected = {"basis": "descriptive", "cells": [{"count": 3}]}
        with patch.object(settings, "AUTH_ENABLED", True), \
             patch.object(portfolio, "get_cost_benefit_service") as service:
            service.return_value.get_segment_matrix.return_value = expected
            self.client.app.dependency_overrides.pop(auth_deps.current_user)
            self.assertEqual(self.client.get("/api/portfolio/matrix").status_code, 401)
            service.assert_not_called()
            self.client.app.dependency_overrides[auth_deps.current_user] = lambda: self.user
            for role in ("staff", "manager", "admin", "viewer"):
                with self.subTest(role=role):
                    self.user.role = role
                    response = self.client.get("/api/portfolio/matrix")
                    self.assertEqual(response.status_code, 403 if role == "staff" else 200)
                    if role != "staff":
                        self.assertEqual(response.json(), expected)
            self.assertEqual(service.call_count, 3)

    def test_readers_cannot_submit_batch_scoring(self):
        for role in ("viewer", "manager", "staff"):
            self.user.role = role
            self.assertEqual(self.client.get("/api/model/batch-score").status_code, 403)

    def test_create_uses_server_snapshot(self):
        response = self.client.post("/api/work-orders", json={
            "customer_id": "C000001", "customer_name": "伪造姓名", "assignee": "tester",
            "probability": 0.01, "balance": 1, "model_used": "伪造模型",
            "thresholds_snapshot": {"high": 0.99}, "risk_factors": ["伪造归因"]})
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["customer_name"], "Test")
        self.assertEqual(data["probability"], .8)
        self.assertEqual(data["balance"], 50000)
        self.assertEqual(data["model_used"], "test")

    def test_invalid_and_exited_customers_rejected(self):
        for cid, code in (("C999999", 404), ("C000003", 409)):
            response = self.client.post("/api/work-orders", json={
                "customer_id": cid, "assignee": "tester"})
            self.assertEqual(response.status_code, code, response.text)
        for field, value in (("probability", 2), ("balance", -1)):
            response = self.client.post("/api/work-orders", json={
                "customer_id": "C000001", "assignee": "tester", field: value})
            self.assertEqual(response.status_code, 422, response.text)

    def test_reopen_rejects_missing_or_exited_customer_without_changes(self):
        with self.sessions() as db:
            db.add_all([WorkOrder(id=57, customer_id="C999999", status="cancelled"),
                        WorkOrder(id=58, customer_id="C000003", status="cancelled")])
            db.commit()
            audits = db.query(AuditLog).count()
        for order_id, code in ((57, 404), (58, 409)):
            for status in ("pending", "in_progress"):
                with self.subTest(order_id=order_id, status=status):
                    response = self.client.put(f"/api/work-orders/{order_id}", json={"status": status})
                    self.assertEqual(response.status_code, code, response.text)
        with self.sessions() as db:
            self.assertEqual(db.get(WorkOrder, 57).status, "cancelled")
            self.assertEqual(db.get(WorkOrder, 58).status, "cancelled")
            self.assertEqual(db.query(AuditLog).count(), audits)

    def test_reopen_clears_outcome_without_rescoring_and_is_audited(self):
        from datetime import datetime
        with self.sessions() as db:
            db.add(WorkOrder(id=57, customer_id="C000001", status="completed",
                             result="retained", completed_at=datetime(2026, 1, 1)))
            db.commit()
        with patch.object(customer_service, "get_customer_detail", side_effect=RuntimeError("model offline")):
            response = self.client.put("/api/work-orders/57", json={"status": "pending"})
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            order = db.get(WorkOrder, 57)
            self.assertEqual(order.status, "pending")
            self.assertIsNone(order.result)
            self.assertIsNone(order.completed_at)
            self.assertIsNotNone(db.query(AuditLog).filter_by(action="update_work_order", target="#57").first())

    def test_terminal_history_correction_does_not_require_retention_eligibility(self):
        response = self.client.put("/api/work-orders/8", json={"status": "lost"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "lost")

    def test_reopen_rolls_back_if_audit_fails(self):
        with self.sessions() as db:
            db.add(WorkOrder(id=57, customer_id="C000001", status="cancelled"))
            db.commit()
        with patch.object(auth_deps, "write_audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.put("/api/work-orders/57", json={"status": "pending"})
        with self.sessions() as db:
            self.assertEqual(db.get(WorkOrder, 57).status, "cancelled")

    def test_concurrent_reopen_keeps_only_one_active_order(self):
        with self.sessions() as db:
            db.add_all([WorkOrder(id=57, customer_id="C000001", status="cancelled"),
                        WorkOrder(id=58, customer_id="C000001", status="cancelled")])
            db.commit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda oid: self.client.put(
                f"/api/work-orders/{oid}", json={"status": "pending"}), (57, 58)))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        with self.sessions() as db:
            self.assertEqual(db.query(WorkOrder).filter(
                WorkOrder.customer_id == "C000001", WorkOrder.status == "pending").count(), 1)
            self.assertEqual(db.query(AuditLog).filter_by(action="update_work_order").count(), 1)

    def test_batch_create_preserves_retention_eligibility(self):
        response = self.client.post("/api/work-orders/batch", json={"items": [
            {"customer_id": cid, "assignee": "tester"}
            for cid in ("C000001", "C000003", "C999999")]})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["succeeded"], 1)
        self.assertEqual(response.json()["failed_count"], 2)
        self.assertEqual([o["customer_id"] for o in response.json()["created"]], ["C000001"])

    def test_seed_candidates_exclude_exited_missing_and_active_customers(self):
        from app import seed_work_orders
        def scored(cid, value):
            return {"customer_id": cid, "surname": "Test", "geography": "France",
                    "risk_level": "HIGH", "probability": .8, "balance": 50000,
                    "risk_factors": [], "action": "联系", "value_tier": "LOW",
                    "expected_value": value, "channel": "outbound"}
        candidates = [scored("C000001", 1), scored("C000003", 100), scored("C999999", 200)]
        with self.sessions() as db:
            db.query(WorkOrder).delete()
            db.commit()
        with patch.object(seed_work_orders, "engine", self.engine), \
             patch.object(seed_work_orders, "SessionLocal", self.sessions), \
             patch.object(seed_work_orders.risk_scoring, "get_scored_customers", return_value=candidates), \
             patch.object(seed_work_orders, "NUM_ORDERS", 3), \
             patch("sys.argv", ["seed_work_orders"]):
            self.assertEqual(seed_work_orders.main(), 0)
        with self.sessions() as db:
            self.assertEqual([o.customer_id for o in db.query(WorkOrder).all()], ["C000001"])

    def test_completed_requires_explicit_observed_result(self):
        response = self.client.put("/api/work-orders/56", json={"status": "completed"})
        self.assertEqual(response.status_code, 422, response.text)
        with self.sessions() as db:
            self.assertEqual(db.get(WorkOrder, 56).status, "in_progress")
        response = self.client.put("/api/work-orders/56", json={"status": "completed", "result": "lost"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "lost")

    def test_batch_is_bound_to_approved_owner_and_one_use(self):
        prepared = self.prepare(["C000001", "C000002", "C000003", "C999999"])
        self.assertEqual(len(prepared["items"]), 2)
        self.assertEqual(len(prepared["failed"]), 2)
        proposals = [self.proposal(item) for item in prepared["items"]]
        tampered = copy.deepcopy(proposals[0])
        tampered["payload"]["assignee"] = "another"
        self.assertEqual(self.confirm(tampered).status_code, 409)
        response = self.client.post("/api/agent/confirm-batch", json={"proposals": proposals})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["succeeded"], 2)
        replay = self.client.post("/api/agent/confirm-batch", json={"proposals": proposals})
        self.assertEqual(replay.json()["succeeded"], 0)
        self.assertEqual(replay.json()["failed_count"], 2)

    def test_batch_without_token_cannot_create(self):
        response = self.client.post("/api/agent/confirm-batch", json={"proposals": [{
            "action": "create_work_order", "payload": {"customer_id": "C000001", "assignee": "tester"}}]})
        self.assertEqual(response.json()["succeeded"], 0)
        with self.sessions() as db:
            self.assertEqual(db.query(WorkOrder).count(), 3)

    def test_display_name_resolves_to_real_assignee(self):
        prepared = self.prepare(assignee="测试经理")
        self.assertEqual(prepared["items"][0]["assignee"], "tester")
        bad = self.prepare(assignee="不存在的经理")
        self.assertEqual(bad["items"], [])
        self.assertEqual(len(bad["failed"]), 1)

    def test_first_person_assignee_is_current_user(self):
        from app.agent import guard_rules
        owner = guard_rules.detect_write_intent_multi("给C000001建单，负责人用我本人")[2]
        self.assertEqual(owner, "我本人")
        prepared = self.prepare(assignee=owner)
        self.assertEqual(prepared["items"][0]["assignee"], "tester")

    def test_ambiguous_assignee_never_silently_defaults_to_requester(self):
        from app.agent import guard_rules
        owner = guard_rules.detect_write_intent_multi("给C000001建单，派给他")[2]
        self.assertEqual(owner, "他")
        self.assertEqual(self.prepare(assignee=owner)["items"], [])

    def test_create_rejects_changed_customer_or_model(self):
        proposal = self.proposal(self.prepare()["items"][0])
        with self.sessions() as db:
            db.query(Customer).filter_by(customer_id="C000001").update({"surname": "Changed"})
            db.commit()
        self.assertEqual(self.confirm(proposal).status_code, 409)
        proposal = self.proposal(self.prepare()["items"][0])
        with patch.object(risk_scoring, "_read_model_version", return_value="new-model"):
            self.assertEqual(self.confirm(proposal).status_code, 409)

    def test_database_prevents_concurrent_duplicate_active_orders(self):
        from sqlalchemy.exc import IntegrityError
        with self.sessions() as db:
            db.add(WorkOrder(customer_id="C071081", status="pending"))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_model_snapshot_and_active_outcome_cannot_be_overwritten(self):
        for payload in ({"risk_level": "LOW"}, {"probability": .99}, {"result": "retained"}):
            response = self.client.put("/api/work-orders/56", json=payload)
            self.assertEqual(response.status_code, 422, response.text)

    def test_changed_followup_filters_restart_cursor_and_preserve_other_constraints(self):
        from langchain_core.messages import AIMessage
        state = {"question": "另外三位余额超过60000的客户", "context_in": {"list_query": {
            "risk_level": "HIGH", "min_balance": 40000, "sort_by": "expected_value",
            "limit": 3, "offset": 100, "next_offset": 103}}, "tool_results": [],
            "messages": [AIMessage(content="", tool_calls=[{"name": "list_customers",
                "args": {"min_balance": 60000, "limit": 3}, "id": "next"}])]}
        with patch.object(tools, "call_tool", return_value={"ok": True, "result": {"items": []}}) as call:
            graph.node_tools(state)
        args = call.call_args.args[1]
        self.assertEqual(args["offset"], 0)
        # ‘超过’必须排除恰好60000；不能把严格边界退化为‘至少’。
        import math
        self.assertEqual(args["min_balance"], math.nextafter(60000, math.inf))
        self.assertEqual(args["risk_level"], "HIGH")
        self.assertEqual(args["sort_by"], "expected_value")

    def test_server_session_is_authoritative_versioned_and_isolated(self):
        result = {"context": {"customers": [{"id": "C000001"}], "orders": [],
                             "focus_customer_id": "C000001"}, "text": "查询结果"}
        with patch.object(graph, "run", return_value=result):
            first = self.client.post("/api/agent/ask", json={"question": "查询", "session_id": "session-a",
                "session_version": 0, "context": {"customers": [{"id": "C000002", "prob": 1}],
                    "list_query": {"offset": 500}}})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["session_version"], 1)
        with patch.object(graph, "run", return_value={"text": "续查"}) as run:
            response = self.client.post("/api/agent/ask", json={"question": "查询", "session_id": "session-a",
                "session_version": 1, "context": {"focus_customer_id": "C999999"}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(run.call_args.kwargs["context"]["focus_customer_id"], "C000001")
        response = self.client.post("/api/agent/ask", json={"question": "查询", "session_id": "session-a",
            "session_version": 1})
        self.assertEqual(response.status_code, 409, response.text)
        self.user.username = "another"
        response = self.client.get("/api/agent/session/session-a")
        self.assertTrue(response.json()["expired"])

    def test_session_busy_and_client_numeric_injection(self):
        with patch.object(graph, "run", return_value={"text": "查询"}) as run:
            response = self.client.post("/api/agent/ask", json={"question": "查询", "session_id": "busy",
                "context": {"customers": [{"id": "C000001", "prob": .999, "balance": 999999}],
                            "list_query": {"offset": 900}}})
        self.assertEqual(response.status_code, 200, response.text)
        ctx = run.call_args.kwargs["context"]
        self.assertIsNone(ctx["customers"][0]["prob"])
        self.assertNotIn("list_query", ctx)
        with self.sessions() as db:
            db.get(order_safety.AgentSession, ("tester", "busy")).busy_until = time.time() + 100
            db.commit()
        self.assertEqual(self.client.post("/api/agent/ask", json={
            "question": "查询", "session_id": "busy"}).status_code, 409)

    def test_negated_create_never_gets_confirmation(self):
        from app.agent import guard_rules
        pending = {"action": "create_work_order", "payload": {"customer_id": "C000001"}}
        for question in ("不要给C000001建单", "如何给C000001建单", "别建单，只查询C000001"):
            self.assertFalse(guard_rules.detect_write_intent_multi(question)[0])
            with patch.object(graph, "run", return_value={"pending_action": pending}):
                response = self.client.post("/api/agent/ask", json={"question": question})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(response.json()["pending_action"])

    def test_customer_filters_and_cursor_apply_before_paging(self):
        scored = [{"customer_id": f"C{i:06}", "surname": "Test", "exited": i % 2,
            "geography": "France", "risk_level": "HIGH", "value_tier": "LOW",
            "balance": float(i), "probability": .8, "expected_value": float(i) * .8}
            for i in range(1, 401)]
        with patch.object(risk_scoring, "get_scored_customers", return_value=scored):
            response = self.client.get("/api/customers", params={"exited": 0,
                "has_order": "false", "min_balance": 100, "offset": 103,
                "page_size": 3, "sort_by": "balance", "sort_order": "asc"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 151)
        self.assertEqual([x["balance"] for x in response.json()["items"]], [306, 308, 310])
        with patch.object(tools, "_get", return_value={"items": scored[300:303], "total": 400}) as get:
            data = tools.list_customers(limit=3, offset=301, min_balance=300)
        self.assertEqual(get.call_args.kwargs["offset"], 301)
        self.assertEqual(data["next_offset"], 304)

    def test_followup_preserves_filter_sort_and_exact_cursor(self):
        ctx = {"list_query": {"risk_level": "HIGH", "min_balance": 40000,
            "sort_by": "expected_value", "limit": 3, "offset": 102, "next_offset": 105,
            "exited": 0, "has_order": False, "worth_only": True}}
        with patch.object(tools, "call_tool", return_value={"ok": True, "result": {
            "items": [], "total": 105, "next_offset": 105}}) as call:
            answer = graph.run("另外三位客户", session_id="followup", context=ctx)
        self.assertEqual(call.call_args.args[1]["offset"], 105)
        self.assertEqual(call.call_args.args[1]["min_balance"], 40000)
        self.assertEqual(call.call_args.args[1]["sort_by"], "expected_value")
        self.assertEqual(answer["basis"], "system_data")

    def test_answer_uses_primary_data_not_auxiliary_tool_or_llm_names(self):
        from app.agent import answer_builder
        records = [{"tool": "list_customers", "ok": True, "data": {"items": [{
            "customer_id": "C000001", "surname": "Test", "exited": 0,
            "probability": .8, "balance": 50000}], "total": 1}},
            {"tool": "get_thresholds", "ok": True, "data": {"high": .3}}]
        answer = answer_builder.build(records, "推荐客户名单", llm_headline="为错误客户 Other 建单")
        self.assertIn("Test", str(answer))
        self.assertNotIn("Other", str(answer))

    def test_multiple_customer_details_all_remain_visible(self):
        from app.agent import answer_builder
        records = [{"tool": "get_customer_risk", "ok": True, "data": {
            "customer_id": cid, "surname": cid, "exited": 0, "balance": 50000,
            "probability": .8, "expected_value": 40000, "risk_factors": ["业务规则"]}}
            for cid in ("C000001", "C000002")]
        answer = answer_builder.build(records, "查询这两位客户")
        self.assertEqual([item["customer_id"] for item in answer["entities"]], ["C000001", "C000002"])
        self.assertTrue(any("C000001" in fact["label"] for fact in answer["facts"]))
        self.assertTrue(any("C000002" in fact["label"] for fact in answer["facts"]))

    def test_verify_binds_metric_entity_units_and_blocks_unparsed_claims(self):
        from app.agent.verify import verify
        records = [{"tool": "list_customers", "ok": True, "data": {"items": [
            {"customer_id": "C000001", "surname": "甲", "probability": .8, "balance": 50000},
            {"customer_id": "C000002", "surname": "乙", "probability": .2, "balance": 20000}]}}]
        self.assertTrue(verify("C000001流失概率80%，C000002余额2万元", records)[0])
        for claim in ("C000001流失概率20%", "C000001余额80元", "C000001余额50000%",
                      "C000001流失概率80元", "流失概率80%", "概率百分之八十", "挽留三人"):
            with self.subTest(claim=claim):
                self.assertFalse(verify(claim, records)[0])
        self.assertFalse(verify("流失概率80%", [], extra_sources={"probability": .8})[0])

    def test_model_selection_and_manifest_are_consistent(self):
        import json
        from app.celery_tasks import train
        results = {"A": {"cv_auc_mean": .9, "auc": .7}, "B": {"cv_auc_mean": .8, "auc": .99}}
        with patch.object(train, "MODEL_DIR", Path(self.tmp.name) / "models"):
            train._save_results_to_disk({"A": {"version": 1}, "B": {"version": 1}}, results,
                                        data_fingerprint="fingerprint")
            root = train.MODEL_DIR
            first = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            train._save_results_to_disk({"A": {"version": 2}, "B": {"version": 2}}, results)
            second = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            self.assertNotEqual(first["version"], second["version"])
            self.assertTrue(all((root / p).is_file() for p in first["model_files"].values()))
            self.assertEqual(risk_scoring.select_model(second), "A")
            self.assertIn(second["version"], str(risk_scoring.model_artifact(second, "A", root)))

    def test_threshold_uses_held_out_fold_predictions(self):
        import numpy as np
        from sklearn.datasets import make_classification
        from sklearn.tree import DecisionTreeClassifier
        from app.celery_tasks import train
        X, y = make_classification(n_samples=160, n_features=6, n_informative=3, random_state=7)
        model = DecisionTreeClassifier(random_state=7)
        threshold, grid, aucs = train._threshold_validation(model, X, y)
        self.assertFalse(hasattr(model, "tree_"))
        self.assertEqual(len(aucs), train.settings.CV_FOLDS)
        self.assertTrue(np.isfinite(aucs).all())
        self.assertIn(threshold, [row["threshold"] for row in grid])
        self.assertTrue(any(row["fp"] or row["fn"] for row in grid))

    def test_logout_revocation_shared_by_middleware_and_dependency(self):
        from app.middleware.auth import AuthMiddleware
        from app.services import auth_service
        from app.config import settings
        app = self.client.app
        app.dependency_overrides.pop(auth_deps.current_user)
        with patch.object(settings, "AUTH_ENABLED", True), \
             patch.object(auth_service, "_jwt_secret", return_value=b"isolated-test-key"):
            with self.sessions() as db:
                user = db.query(User).filter_by(username="tester").one()
                token = auth_service.make_session_token(db, user)
                other = auth_service.make_session_token(db, user)
                db.commit()
            headers = {"Authorization": "Bearer " + token}
            self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
            response = self.client.post("/api/auth/logout", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 401)
            middleware_app = FastAPI()
            middleware_app.include_router(auth_router.router)
            middleware_app.add_middleware(AuthMiddleware)
            with TestClient(middleware_app) as second:
                self.assertEqual(second.get("/api/auth/me", headers=headers).status_code, 401)
                self.assertEqual(second.get("/api/auth/me", headers={
                    "Authorization": "Bearer " + other}).status_code, 200)

    def test_real_training_pipeline_and_shap_binary_array_shape(self):
        from sklearn.datasets import make_classification
        from sklearn.model_selection import train_test_split
        from sklearn.ensemble import RandomForestClassifier
        from app.celery_tasks import train
        X, y = make_classification(n_samples=220, n_features=len(train.FEATURE_NAMES),
            n_informative=5, random_state=9)
        X_train, X_test, y_train, y_test = train_test_split(X, y, stratify=y, random_state=9)
        for name, model in train._get_models().items():
            result = train._train_and_evaluate(model, name, X_train, X_test, y_train, y_test)
            self.assertEqual(result["threshold_basis"], "out_of_fold_training_predictions")
            self.assertEqual(sum(result["confusion_matrix"].values()), len(y_test))
        forest = RandomForestClassifier(n_estimators=8, random_state=9).fit(X_train, y_train)
        explanation = train._compute_shap({"Forest": forest}, X_test[:4], y_test[:4])
        self.assertNotIn("error", explanation["Forest"], str(explanation))
        self.assertEqual(set(explanation["Forest"]), set(train.FEATURE_NAMES))


if __name__ == "__main__":
    unittest.main()
