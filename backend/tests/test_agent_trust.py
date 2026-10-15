"""智能体指令与公开内容回归；沿用隔离fixture，不调用真实LLM。"""
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from app.agent import guard_rules, graph, tools, order_safety, verify, answer_builder
import test_agent_order_safety as fixtures


class AgentTrustTests(unittest.TestCase):
    setUp = fixtures.OrderSafetyTests.setUp
    tearDown = fixtures.OrderSafetyTests.tearDown
    ask = fixtures.OrderSafetyTests.ask

    def test_production_agent_error_does_not_expose_internal_details(self):
        with patch.object(graph, 'run', side_effect=RuntimeError('private-key-secret')), \
                patch.object(graph.settings, 'DEBUG', False):
            response=self.client.post('/api/agent/ask',json={'question':'查询C000001风险'})
        self.assertEqual(response.status_code,500)
        self.assertNotIn('private-key-secret',response.text)

    def test_read_only_negated_create_and_inquiry_are_not_write_intents(self):
        for question in (
            "再给我另外十二位挽回价值最高的客户，排除刚才三位，只查询，不创建工单。",
            "暂不为C000001创建工单", "无需给C000001建单", "C000001要不要建单",
            "取消建单", "给C000001建单只是示例，不执行", "只查询C000001的工单",
        ):
            with self.subTest(question=question):
                self.assertFalse(guard_rules.detect_write_intent_multi(question)[0])

    def test_negative_cancel_does_not_deny_positive_create_or_add_its_target(self):
        intent, ids, _ = guard_rules.detect_write_intent_multi(
            "不要取消C000001原工单，但给C000002创建工单")
        self.assertTrue(intent)
        self.assertEqual(ids, ["C000002"])
        self.assertIsNone(order_safety.operation("不要取消原工单，但给C000002创建工单"))

    def test_llm_write_tool_is_denied_on_read_only_question(self):
        state = {"question": "仅查询C000001的风险，不创建工单", "tool_results": [],
                 "messages": [AIMessage(content="", tool_calls=[{"name": "propose_create_work_order",
                     "args": {"customer_id": "C000001"}, "id": "create"}])]}
        with patch.object(tools, "call_tool") as call:
            result = graph.node_tools(state)
        call.assert_not_called()
        self.assertIsNone(result["pending_action"])

    def test_router_rejects_negated_create_proposal_before_issuing_token(self):
        proposal = {"action": "create_work_order", "payload": {
            "customer_id": "C000001", "customer_name": "Test", "assignee": "tester"}}
        with patch.object(graph, "run", return_value={"pending_action": proposal}):
            result = self.ask("只查询C000001，不创建工单")
        self.assertTrue(result["pending_action"] is None, "否定请求产生了待确认动作")
        with self.sessions() as db:
            self.assertEqual(db.query(order_safety.PendingAgentAction).count(), 0)

    def test_mixed_clause_uses_only_positive_action_owner(self):
        intent, ids, owner = guard_rules.detect_write_intent_multi(
            "不要给C000001建单，负责人张三，但给C000002建单，负责人tester")
        self.assertTrue(intent)
        self.assertEqual(ids, ["C000002"])
        self.assertEqual(owner, "tester")

    def test_positive_create_in_mixed_request_has_confirmation_without_creating_order(self):
        from app.models.work_order import WorkOrder
        proposal = {"action": "create_work_order", "payload": {
            "customer_id": "C000002", "customer_name": "Second", "assignee": "tester"}}
        with patch.object(graph, "run", return_value={"pending_action": proposal}):
            result = self.ask("不要取消C000001原工单，但给C000002创建工单")
        self.assertEqual(result["pending_action"]["payload"]["customer_id"], "C000002")
        with self.sessions() as db:
            self.assertEqual(db.query(WorkOrder).count(), 3)
            self.assertEqual(db.query(order_safety.PendingAgentAction).count(), 1)

    def test_mixed_request_does_not_authorize_denied_customer(self):
        question = "不要给C000001建单，但给C000002建单"
        self.assertIsNotNone(graph._scope_error({"question": question},
            "propose_create_work_order", {"customer_id": "C000001"}, []))


class FactVerificationTests(unittest.TestCase):
    def setUp(self):
        self.records = [{"tool": "list_customers", "ok": True, "data": {"items": [
            {"customer_id": "C000001", "surname": "甲", "probability": .8, "balance": 50000,
             "risk_level": "HIGH", "expected_value": 40000},
            {"customer_id": "C000002", "surname": "乙", "probability": .2, "balance": 20000,
             "risk_level": "LOW", "expected_value": 4000}], "returned": 2,
             "matched_total": 20, "model_version": "fixture-version"}}]

    def test_unknown_entity_and_cross_entity_claims_are_rejected(self):
        for claim in ("C999999流失概率80%", "C000001和C000002流失概率分别20%和80%",
                      "C000001的姓名为乙", "C000001为低风险", "C000002为高危",
                      "C000001为HIGH，C000002为HIGH", "C000001和C000002均为高危"):
            with self.subTest(claim=claim):
                self.assertFalse(verify.verify(claim, self.records)[0])

    def test_clear_entity_metric_units_and_rounding_are_preserved(self):
        for claim in ("C000001流失概率80%，余额5万元", "甲流失概率80.0%，乙流失概率20%",
                      "C000001为高危，C000002为低风险", "C000001的姓名为甲"):
            with self.subTest(claim=claim):
                self.assertTrue(verify.verify(claim, self.records)[0])

    def test_failed_interpretation_is_absent_from_all_public_answer_fields(self):
        state = {"question": "推荐客户名单", "tool_results": self.records,
                 "headline": "C000001流失概率99%", "insights": [],
                 "draft": "C000001流失概率99%"}
        result = graph.node_verify(state)
        self.assertFalse(result["verify_passed"])
        self.assertNotIn("99%", result["final"])
        self.assertEqual(result["final"], answer_builder.to_text(result["answer"]))
        with patch.object(graph, "get_graph") as compiled:
            compiled.return_value.invoke.return_value = {**result, "tool_results": self.records}
            public = graph.run(state["question"])
        import json
        self.assertNotIn("99%", json.dumps(public, ensure_ascii=False))
        self.assertTrue(public["answer"]["entities"])
        self.assertTrue(public["answer"]["warning"])

    def test_valid_interpretation_and_plain_greeting_remain_visible(self):
        state = {"question": "推荐客户名单", "tool_results": self.records,
                 "headline": "C000001流失概率80%", "insights": ["C000002流失概率20%"],
                 "draft": "正确数据"}
        result = graph.node_verify(state)
        self.assertTrue(result["verify_passed"])
        self.assertIn("20%", result["final"])
        greeting = graph.node_verify({"question": "你好", "tool_results": [],
            "headline": "", "insights": [], "direct_answer": "你好，我可以帮助你查询业务数据。"})
        self.assertEqual(greeting["final"], "你好，我可以帮助你查询业务数据。")


if __name__ == "__main__":
    unittest.main()
