"""Agent query regressions using pure functions and mocked model/tool calls."""
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage

from app.agent import context, graph, guard_rules, list_queries, tools
from app.services import customer_service, risk_scoring


def customer(number):
    return {"customer_id": f"C{number:06}", "surname": "Fixture",
            "probability": .4, "balance": 100000, "expected_value": 40000,
            "risk_level": "HIGH", "value_tier": "LOW", "exited": 0,
            "has_active_order": False, "channel": "outbound", "action": "fixture"}


def displayed_context(size=50):
    record = {"tool": "list_customers", "ok": True, "data": {
        "items": [customer(i) for i in range(1, size + 1)],
        "next_offset": size, "model_version": "fixture-v1"}}
    return context.normalize(context.merge(context.empty(), context.extract([record], None)))


def proposal(name, args):
    """替身必须遵守**真实工具契约**（E01 起新增 __pending_action__ 形状校验）。"""
    if name == "propose_create_work_orders_batch":
        return {"ok": True, "result": {"__pending_action__": True,
            "action": "create_work_order_batch",
            "items": [{"customer_id": cid, "customer_name": "Fixture"}
                      for cid in args["customer_ids"]], "requires_confirmation": True}}
    return {"ok": True, "result": {"__pending_action__": True,
        "action": "create_work_order", "payload": {
        "customer_id": args["customer_id"], "customer_name": "Fixture"},
        "requires_confirmation": True}}


class CreateTargetRepairs(unittest.TestCase):
    def test_fallback_respects_single_ordinal_beyond_detail_cache(self):
        for position in (2, 25, 50):
            with self.subTest(position=position), patch.object(tools, "call_tool", side_effect=proposal) as call:
                result = graph.node_synthesis({"question": f"给第{position}位建单",
                    "context_in": displayed_context()})
                self.assertEqual(call.call_args.args,
                    ("propose_create_work_order", {"customer_id": f"C{position:06}", "assignee": ""}))
                self.assertEqual(result["pending_action"]["payload"]["customer_id"], f"C{position:06}")

    def test_ambiguous_singular_and_partial_group_do_not_expand(self):
        for question in ("给他建单", "给这三位建单", "给第2位和第3位建单"):
            with self.subTest(question=question), patch.object(tools, "call_tool") as call:
                with self.assertRaises(ValueError):
                    graph.node_synthesis({"question": question, "context_in": displayed_context()})
                call.assert_not_called()

    def test_resolver_preserves_explicit_and_exact_group_targets(self):
        self.assertEqual(context.resolve_create_targets("给C000025建单", displayed_context()), ["C000025"])
        ctx = displayed_context(3)
        self.assertEqual(context.resolve_create_targets("给这三位建单", ctx), ctx["displayed_customer_ids"])
        ctx["focus_customer_id"] = "C000002"
        self.assertEqual(context.resolve_create_targets("给他建单", ctx), ["C000002"])

    def test_explicit_id_cannot_contradict_the_ordinal(self):
        with self.assertRaises(ValueError):
            context.resolve_create_targets("给第25位C000002建单", displayed_context())

    def test_normal_tool_path_rejects_wrong_or_ambiguous_targets(self):
        state = {"question": "给他建单", "context_in": displayed_context(), "messages": [
            AIMessage(content="", tool_calls=[{"name": "propose_create_work_orders_batch",
                "args": {"customer_ids": ["C000001", "C000002"]}, "id": "fixture"}])]}
        with patch.object(tools, "call_tool") as call:
            result = graph.node_tools(state)
        call.assert_not_called()
        self.assertFalse(result["tool_results"][0]["ok"])


class ContinuationRepairs(unittest.TestCase):
    def setUp(self):
        self.previous = {"sort_by": "expected_value", "exited": 0, "has_order": False,
            "worth_only": False, "geography": "Germany", "risk_level": "HIGH",
            "min_balance": 100000, "limit": 3, "next_offset": 3,
            "seen_customer_ids": ["C000001", "C000002", "C000003"], "model_version": "fixture-v1"}

    def test_new_balance_bound_and_cleared_geography_restart_range(self):
        for question, key, expected in (("再来三位余额至少50万的客户", "min_balance", 500000),
                ("下一批不限制地区，其他条件不变", "geography", None)):
            with self.subTest(question=question):
                args, changed = list_queries.continuation(self.previous, question)
                self.assertEqual(args.get(key), expected)
                self.assertTrue(changed)
                self.assertEqual(args["offset"], 0)
                self.assertNotIn("exclude_ids", args)

    def test_negated_region_and_risk_do_not_override_positive_filters(self):
        for question, key, expected in (("再来三位法国客户，不要德国客户", "geography", "France"),
                ("再来三位低风险客户，不要高风险客户", "risk_level", "LOW")):
            with self.subTest(question=question):
                args, changed = list_queries.continuation(self.previous, question)
                self.assertEqual(args[key], expected)
                self.assertTrue(changed)

    def test_unrepresentable_filters_are_explicitly_rejected(self):
        for question in ("再来三位非德国客户", "再来三位法国或西班牙客户", "再来三位年龄超过40岁的客户",
                "下一批三位余额在20万到30万的客户", "下一批三位余额介于20万和30万之间",
                "下一批三位余额至少20万且最多30万的客户"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                list_queries.continuation(self.previous, question)

    def test_strict_and_inclusive_balance_bounds_filter_actual_boundary_values(self):
        rows = [{**customer(i), "balance": balance} for i, balance in enumerate(
            (50000.0, 60000.0, 70000.0), 1)]
        cases = (("余额超过60000", ["C000003"]), ("余额至少60000", ["C000002", "C000003"]),
                 ("余额小于60000", ["C000001"]), ("余额不超过60000", ["C000001", "C000002"]),
                 ("余额至少50000且小于70000", ["C000001", "C000002"]),
                 ("余额超过50000且至多60000", ["C000002"]))
        with patch.object(risk_scoring, "get_scored_customers", return_value=rows), \
                patch.object(customer_service, "_active_order_ids", return_value=set()):
            for question, expected in cases:
                with self.subTest(question=question):
                    filters = list_queries.requested_changes(question)
                    matched, _ = customer_service._filtered_customers(None, **filters)
                    self.assertEqual([row["customer_id"] for row in matched], expected)

    def test_unchanged_filters_keep_cursor_and_model(self):
        args, changed = list_queries.continuation(self.previous, "另外三位，按风险资产评分")
        self.assertFalse(changed)
        self.assertEqual(args["exclude_ids"], self.previous["seen_customer_ids"])
        self.assertEqual(args["expected_version"], "fixture-v1")

    def test_work_order_topic_does_not_use_customer_cursor(self):
        compiled = Mock()
        compiled.invoke.return_value = {"final": "fixture", "answer": {}, "answer_basis": "system_meta"}
        with patch.object(graph, "get_graph", return_value=compiled), patch.object(tools, "call_tool") as call:
            graph.run("下一批工单", context={"list_query": self.previous})
        call.assert_not_called()
        compiled.invoke.assert_called_once()

    def test_work_order_topic_is_not_reclassified_by_a_read_only_suffix(self):
        self.assertFalse(list_queries.is_customer_continuation("下一批工单，不创建工单"))
        self.assertTrue(list_queries.is_customer_continuation("下一批三位没有在途工单的客户，不创建工单"))

    def test_actual_word_does_not_swallow_write_or_customer_scope(self):
        for question in ("实际给C000001创建挽留工单", "查看C000001的实际挽留结果", "这位客户实际回访结果是什么",
                "他的实际回访情况", "查看实际回访情况"):
            with self.subTest(question=question):
                compiled = Mock()
                compiled.invoke.return_value = {"final": "fixture", "answer": {}, "answer_basis": "system_meta"}
                with patch.object(graph, "get_graph", return_value=compiled), patch.object(tools, "call_tool") as call:
                    graph.run(question, context={"list_query": self.previous, "focus_customer_id": "C000001"})
                call.assert_not_called()
                compiled.invoke.assert_called_once()

    def test_explicit_global_summary_remains_available_with_a_customer_focus(self):
        result = {"records": 0, "note": "fixture"}
        with patch.object(tools, "call_tool", return_value={"ok": True, "result": result}) as call, \
                patch.object(graph, "get_graph") as compiled:
            graph.run("查看全局实际工单挽留结果汇总", context={"focus_customer_id": "C000001"})
        compiled.assert_not_called()
        self.assertEqual(call.call_args.args, ("get_retention_summary", {}))


class GuardAndWriterRepairs(unittest.TestCase):
    def test_negated_prediction_and_continuation_are_answerable(self):
        for question in ("我不需要预测未来，只查当前德国三位未流失客户", "接下来再来三位客户"):
            with self.subTest(question=question):
                self.assertIsNone(guard_rules.check(question))
        self.assertIsNotNone(guard_rules.check("明年流失率会是多少"))

    def test_negated_prediction_does_not_disable_other_guard_topics(self):
        self.assertIsNotNone(guard_rules.check("不需要预测未来，哪个策略更有效"))
        self.assertIsNotNone(guard_rules.check("不需要预测未来哪个策略更有效"))

    def test_writer_failure_preserves_cards_and_warns_without_verified_llm_label(self):
        for response in (TimeoutError("private upstream failure"), AIMessage(content="[]"),
                         AIMessage(content=["unexpected content block"])):
            with self.subTest(response=repr(response)):
                router = Mock()
                router.bind_tools.return_value = router
                router.invoke.return_value = AIMessage(content="", tool_calls=[{
                    "name": "list_customers", "args": {"limit": 1}, "id": "fixture"}])
                writer = Mock()
                if isinstance(response, Exception):
                    writer.invoke.side_effect = response
                else:
                    writer.invoke.return_value = response
                result = {"items": [customer(1)], "returned": 1, "matched_total": 1,
                          "next_offset": 1, "model_version": "fixture-v1", "is_complete": True}
                with patch.object(graph.llm, "router_llm", return_value=router), \
                        patch.object(graph.llm, "writer_llm", return_value=writer), \
                        patch.object(tools, "call_tool", return_value={"ok": True, "result": result}):
                    answer = graph.run("列出一位未流失客户，只查询")
                self.assertEqual(answer["answer"]["entities"][0]["customer_id"], "C000001")
                self.assertTrue(answer["answer"].get("warning"))
                self.assertNotEqual(answer["basis"], "llm_verified")
                self.assertNotIn("private upstream failure", str(answer))

    def test_update_schema_exposes_explicit_result_without_default(self):
        schema = next(it["function"] for it in graph._tool_schemas()
                      if it["function"]["name"] == "propose_update_work_order")
        self.assertEqual(schema["parameters"]["properties"]["result"]["enum"], ["retained", "lost"])
        self.assertNotIn("default", schema["parameters"]["properties"]["result"])

    def test_completion_result_cannot_be_invented_or_taken_from_a_denied_clause(self):
        payload = {"status": "completed", "result": "retained", "customer_id": "C000001"}
        for question in ("把C000001标记为已完成", "把C000001标记为已完成，不要填挽留成功",
                "C000002已挽留；把C000001标记为已完成", "把C000001标记为已完成，是否挽留成功？",
                "把C000001标记为已完成，已流失"):
            with self.subTest(question=question):
                self.assertIsNotNone(guard_rules.completion_result_error(question, payload))
        self.assertIsNone(guard_rules.completion_result_error(
            "把C000001标记为已完成，结果为挽留成功", payload))
        self.assertIsNone(guard_rules.completion_result_error(
            "把C000001标记为已完成，已流失", {**payload, "result": "lost"}))

    def test_model_supplied_retained_requires_a_user_result_statement(self):
        question = "把C000001标记为已完成"
        error = graph._scope_error({"question": question}, "propose_update_work_order",
            {"order_id": 1, "customer_id": "C000001", "status": "completed", "result": "retained"}, [])
        self.assertIsNotNone(error)
        self.assertIn("结果", error)


if __name__ == "__main__":
    unittest.main()
