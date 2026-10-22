"""E01 回归 —— 统一工具错误契约。

覆盖 docs/AGENT_REMEDIATION_HANDOFF_2026-10-23.md 的 E01-T01…T11。
全部使用替身与纯函数，不连接正式库、不发真实网络请求。
"""
import json
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage

from app.agent import context, graph, tools, verify


def customer(cid="C000001", **kw):
    base = {"customer_id": cid, "surname": "Fixture", "geography": "Germany",
            "probability": 0.8, "balance": 275100.3125, "expected_value": 220080.25,
            "risk_level": "CRITICAL", "value_tier": "HIGH", "exited": 0,
            "has_active_order": False, "channel": "outbound", "action": "fixture",
            "risk_factors": []}
    base.update(kw)
    return base


def tool_call_state(question, name, args):
    return {"question": question, "context_in": context.empty(), "messages": [
        AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "fixture"}])]}


class SingleProposalContract(unittest.TestCase):
    def test_exited_and_active_order_customers_are_explicit_business_rejections(self):
        """E01-T01：已流失 / 已有在途工单 → 明确业务原因，且不产生待确认动作。"""
        cases = [
            (customer(exited=1), "CUSTOMER_EXITED"),
            (customer(has_active_order=True), "ACTIVE_ORDER_EXISTS"),
        ]
        for payload, expected in cases:
            with self.subTest(code=expected), patch.object(tools, "_get", return_value=payload):
                r = tools.call_tool("propose_create_work_order", {"customer_id": "C000001"})
                self.assertFalse(r["ok"])
                self.assertEqual(r["error_code"], expected)
                self.assertFalse(r["retryable"])
                self.assertIn("工单" if expected == "ACTIVE_ORDER_EXISTS" else "流失", r["error"])
                self.assertNotIn("pending_action", r)
                self.assertFalse(tools.validate_pending(r.get("result"))[0])

    def test_failed_write_is_recorded_and_produces_deterministic_error(self):
        """E01-T01/T04：失败必须入 tool_results，并由错误码生成回答。"""
        with patch.object(tools, "_get", return_value=customer(exited=1)) as get:
            state = tool_call_state("给C000001建单", "propose_create_work_order",
                                    {"customer_id": "C000001"})
            out = graph.node_tools(dict(state))
            self.assertEqual(len(out["tool_results"]), 1)
            self.assertFalse(out["tool_results"][0]["ok"])
            self.assertEqual(out["tool_results"][0]["data"]["error_code"], "CUSTOMER_EXITED")
            self.assertIsNone(out["pending_action"])

            merged = {**state, **out}
            answer = graph.node_synthesis(merged)
            self.assertIn("error_answer", answer)
            self.assertIn("已流失", answer["error_answer"]["text"])
            self.assertEqual(answer["error_answer"]["error"]["code"], "CUSTOMER_EXITED")
            # 确定性兜底不得重复调用同一个注定失败的提议
            self.assertEqual(get.call_count, 1)

    def test_fallback_path_records_failure_instead_of_only_logging(self):
        """E01-T04/T06：合成兜底的确定性提议失败也要留痕。"""
        with patch.object(tools, "_get", return_value=customer(has_active_order=True)):
            out = graph.node_synthesis({"question": "给C000001建单",
                                        "context_in": context.empty()})
        self.assertIn("error_answer", out)
        self.assertEqual(len(out["tool_results"]), 1)
        self.assertEqual(out["tool_results"][0]["data"]["error_code"], "ACTIVE_ORDER_EXISTS")


class BatchContract(unittest.TestCase):
    def test_batch_with_no_buildable_customer_is_not_wrapped_as_success(self):
        """E01-T02：批量全部不可建 → call_tool.ok=False，绝不 ok=True + 错误对象。"""
        with patch.object(tools, "_get",
                          return_value=customer(has_active_order=True)):
            r = tools.call_tool("propose_create_work_orders_batch",
                                {"customer_ids": ["C000001", "C000002"]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "BATCH_NO_ITEMS")
        self.assertEqual(len(r["details"]["failed"]), 2)
        self.assertNotIn("__error__", json.dumps(r, ensure_ascii=False))

    def test_batch_partial_success_reports_both_counts(self):
        """E01-T03：部分成功 → 数量与对象准确，逐项原因可见。"""
        def fake_get(path, **params):
            if path.endswith("C000002"):
                return customer("C000002", has_active_order=True)
            return customer("C000001")

        with patch.object(tools, "_get", side_effect=fake_get):
            r = tools.call_tool("propose_create_work_orders_batch",
                                {"customer_ids": ["C000001", "C000002"]})
        self.assertTrue(r["ok"])
        pending = r["result"]
        self.assertEqual([it["customer_id"] for it in pending["items"]], ["C000001"])
        self.assertEqual(pending["failed"][0]["customer_id"], "C000002")
        self.assertEqual(pending["failed"][0]["reason"], "已有在途工单")
        self.assertIn("可准备 1 位", pending["summary"])
        self.assertIn("不可准备 1 位", pending["summary"])
        self.assertTrue(tools.validate_pending(pending)[0])

    def test_batch_limits_dedup_and_remaining(self):
        """E01-T08：0/20/21/50/51、重复编号与余量。"""
        def echo_get(path, **params):
            return customer(path.rstrip("/").split("/")[-1])

        with patch.object(tools, "_get", side_effect=echo_get):
            for size, code in ((0, "INVALID_ARGUMENT"), (51, "BATCH_LIMIT_EXCEEDED")):
                with self.subTest(size=size):
                    ids = [f"C{i:06d}" for i in range(1, size + 1)]
                    r = tools.call_tool("propose_create_work_orders_batch", {"customer_ids": ids})
                    self.assertFalse(r["ok"])
                    self.assertEqual(r["error_code"], code)

            r = tools.call_tool("propose_create_work_orders_batch",
                                {"customer_ids": [f"C{i:06d}" for i in range(1, 22)]})
            self.assertEqual(len(r["result"]["items"]), 20)
            self.assertEqual(r["result"]["remaining_customer_ids"], ["C000021"])

            r = tools.call_tool("propose_create_work_orders_batch",
                                {"customer_ids": [f"C{i:06d}" for i in range(1, 51)]})
            self.assertEqual(len(r["result"]["items"]), 20)
            self.assertEqual(len(r["result"]["remaining_customer_ids"]), 30)

            r = tools.call_tool("propose_create_work_orders_batch",
                                {"customer_ids": ["C000001", "C000001", "C000002"]})
            self.assertEqual([it["customer_id"] for it in r["result"]["items"]],
                             ["C000001", "C000002"])

    def test_first_batch_all_failed_keeps_remaining_without_empty_confirmation(self):
        """E01-T08：首 20 位全失败但仍有下一批 → 不签发空确认、不丢剩余名单。"""
        def fake_get(path, **params):
            return customer(has_active_order=True)

        ids = [f"C{i:06d}" for i in range(1, 22)]
        with patch.object(tools, "_get", side_effect=fake_get):
            r = tools.call_tool("propose_create_work_orders_batch", {"customer_ids": ids})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "BATCH_NO_ITEMS")
        self.assertEqual(r["details"]["remaining_customer_ids"], ["C000021"])
        self.assertEqual(len(r["details"]["failed"]), 20)
        self.assertIsNone(r.get("result"))


class PendingShapeContract(unittest.TestCase):
    def test_malformed_pending_is_rejected(self):
        """E01-T05：None / __error__ / 畸形 pending 都不签凭据。"""
        for bad in (None, {"__error__": "boom"},
                    {"__pending_action__": True, "action": "create_work_order"},
                    {"__pending_action__": True, "action": "unknown_action",
                     "payload": {"customer_id": "C000001"}, "requires_confirmation": True},
                    {"__pending_action__": True, "action": "create_work_order_batch",
                     "items": [], "requires_confirmation": True},
                    {"__pending_action__": True, "action": "update_work_order",
                     "payload": {"customer_id": "C000001"}, "requires_confirmation": True}):
            with self.subTest(bad=bad):
                self.assertFalse(tools.validate_pending(bad)[0])

    def test_none_and_legacy_error_shapes_become_failures(self):
        """E01-T05：工具返回 None 或旧 __error__ 时不得冒充成功。"""
        with patch.dict(tools.TOOL_SPECS, {"fixture_none": {"fn": lambda: None,
                                                            "write": False,
                                                            "label": "", "description": ""}}):
            r = tools.call_tool("fixture_none", {})
            self.assertFalse(r["ok"])
            self.assertEqual(r["error_code"], "EMPTY_TOOL_RESULT")
        with patch.dict(tools.TOOL_SPECS, {"fixture_error": {"fn": lambda: {"__error__": "nope"},
                                                             "write": False,
                                                             "label": "", "description": ""}}):
            r = tools.call_tool("fixture_error", {})
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "nope")

    def test_write_tool_with_malformed_result_never_becomes_pending(self):
        """E01-T05：写工具返回畸形结果 → 记失败，不产生确认按钮。"""
        state = tool_call_state("给C000001建单", "propose_create_work_order",
                                {"customer_id": "C000001"})
        with patch.object(tools, "call_tool",
                          return_value={"ok": True, "result": {"__pending_action__": True}}):
            out = graph.node_tools(dict(state))
        self.assertIsNone(out["pending_action"])
        self.assertEqual(out["tool_results"][0]["data"]["error_code"],
                         "INVALID_PENDING_ACTION")


class FailureSemantics(unittest.TestCase):
    def test_read_success_plus_write_failure_keeps_cards_and_states_reason(self):
        """E01-T06：查询成功 + 建单提议失败 → 保留数据卡片，同时说明未准备成功。"""
        ok_record = {"tool": "get_customer_risk", "args": {"customer_id": "C000001"},
                     "ok": True, "data": customer()}
        failed = {"tool": "propose_create_work_order", "args": {"customer_id": "C000001"},
                  "ok": False, "data": {"error": "该客户已有在途工单，请先处理现有工单。",
                                        "error_code": "ACTIVE_ORDER_EXISTS",
                                        "retryable": False}}
        out = graph.node_verify({"tool_results": [ok_record, failed], "question": "查C000001并建单",
                                 "headline": "", "insights": []})
        self.assertEqual(out["answer"]["entities"][0]["customer_id"], "C000001")
        self.assertIn("未准备", out["answer"]["warning"]["text"])
        self.assertIn("在途工单", out["answer"]["warning"]["text"])

    def test_failed_records_never_become_scope_or_fact_sources(self):
        """E01-T07：失败记录里的编号/余额不得成为授权或事实来源。"""
        failed = {"tool": "list_customers", "args": {"limit": 5}, "ok": False,
                  "data": {"customer_id": "C099999", "balance": 999999,
                           "error": "x", "error_code": "TIMEOUT", "retryable": True}}
        allowed = context.allowed_customer_ids(context.empty(), "查一下", [failed])
        self.assertNotIn("C099999", allowed)
        ok, errors = verify.verify("C099999 的余额是 999999 元", [failed])
        self.assertFalse(ok)
        self.assertIn("unknown_entity", errors)
        self.assertEqual(context._facts_from_record(failed, 0.0), [])

    def test_sensitive_text_in_exception_never_reaches_output(self):
        """E01-T09：异常里的敏感串不会进入 error / 工具留痕。"""
        secret = "sk-live-SECRET-4f2a http://10.1.2.3:8000/internal"
        with patch.object(tools, "_get", side_effect=RuntimeError(secret)):
            r = tools.call_tool("list_customers", {"limit": 2})
        self.assertFalse(r["ok"])
        self.assertNotIn("SECRET", json.dumps(r, ensure_ascii=False))
        self.assertNotIn("10.1.2.3", json.dumps(r, ensure_ascii=False))

        state = tool_call_state("查一下待处理工单", "list_work_orders", {"limit": 3})
        with patch.object(tools, "_get", side_effect=RuntimeError(secret)):
            out = graph.node_tools(dict(state))
        self.assertNotIn("SECRET", json.dumps(out, ensure_ascii=False))
        self.assertNotIn("10.1.2.3", json.dumps(out, ensure_ascii=False))

    def test_empty_success_and_total_failure_are_different(self):
        """E01-T11：成功空查询 ≠ 全部工具失败。"""
        empty = {"tool": "list_customers", "args": {"limit": 3}, "ok": True,
                 "data": {"items": [], "returned": 0, "matched_total": 0,
                          "is_complete": True}}
        built = graph.answer_builder.build([empty], "列三位客户")
        self.assertEqual(built["kind"], "customer_list")
        self.assertEqual(built["entities"], [])
        self.assertNotIn("失败", str(built.get("warning")))

        failed_only = {"tool": "list_customers", "args": {"limit": 3}, "ok": False,
                       "data": {"error": "系统服务暂时不可用，请稍后重试。",
                                "error_code": "UPSTREAM_UNAVAILABLE", "retryable": True}}
        out = graph.node_synthesis({"question": "列三位客户", "tool_results": [failed_only],
                                    "context_in": context.empty()})
        self.assertIn("error_answer", out)
        self.assertEqual(out["error_answer"]["error"]["code"], "UPSTREAM_UNAVAILABLE")
        self.assertTrue(out["error_answer"]["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
