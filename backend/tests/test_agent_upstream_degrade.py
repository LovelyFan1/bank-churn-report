"""E02 回归 —— 决策模型异常的可控降级。

对应 docs/AGENT_REMEDIATION_HANDOFF_2026-10-23.md 的 E02-T01…T11。
不调用真实模型；用替身注入超时/连接失败，验证图的路由与收尾。
"""
import time
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage

from app.agent import context, graph, llm, tools
from app.agent.state import initial_state


def customer(cid="C000001"):
    return {"customer_id": cid, "surname": "Fixture", "probability": .8,
            "balance": 275100.3125, "expected_value": 220080.25,
            "risk_level": "CRITICAL", "value_tier": "HIGH", "exited": 0,
            "has_active_order": False, "channel": "outbound", "action": "fixture",
            "risk_factors": []}


def ok_record(data=None):
    return {"tool": "get_customer_risk", "args": {"customer_id": "C000001"},
            "ok": True, "data": data or customer()}


class UpstreamClassification(unittest.TestCase):
    def test_only_upstream_failures_are_classified(self):
        """E02-T10/T11：编程错误不得被伪装成"上游不可用"。"""
        self.assertEqual(llm.classify_upstream_error(TimeoutError("x"))["code"],
                         "upstream_timeout")
        self.assertEqual(llm.classify_upstream_error(ConnectionError("x"))["code"],
                         "upstream_unavailable")
        self.assertEqual(llm.classify_upstream_error(OSError("x"))["code"],
                         "upstream_unavailable")
        for exc in (KeyError("x"), TypeError("x"), ValueError("x"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__):
                self.assertIsNone(llm.classify_upstream_error(exc))

    def test_no_error_text_leaks_into_classification(self):
        info = llm.classify_upstream_error(TimeoutError("internal 10.0.0.9 secret"))
        self.assertNotIn("10.0.0.9", info["message"])
        self.assertNotIn("secret", info["message"])


class DecisionFailure(unittest.TestCase):
    def _run(self, router):
        with patch.object(graph.llm, "router_llm", return_value=router) as factory, \
                patch.object(graph.llm, "writer_llm") as writer:
            return graph.run("列出三位未流失客户"), factory, writer

    def test_first_call_timeout_returns_safe_unavailable_answer(self):
        """E02-T01：首次调用超时 → 不返回通用 500，也不编造事实。"""
        router = Mock()
        router.bind_tools.return_value = router
        router.invoke.side_effect = TimeoutError("upstream secret")
        result, _, writer = self._run(router)
        self.assertEqual(result["basis"], "upstream_unavailable")
        self.assertIsNone(result["pending_action"])
        self.assertEqual(result["answer"]["entities"], [])
        self.assertEqual(result["answer"]["error"]["code"], "upstream_timeout")
        self.assertEqual(result["answer"]["error"]["retryable"], True)
        text = result["text"]
        for banned in ("已经查询", "已经建单", "没有匹配", "暂无", "secret"):
            self.assertNotIn(banned, text)
        writer.assert_not_called()
        writer.return_value.invoke.assert_not_called()

    def test_client_build_and_bind_failures_enter_the_same_degrade(self):
        """E02-T02：模型初建、绑定工具阶段的异常同样降级。"""
        with patch.object(graph.llm, "router_llm",
                          side_effect=TimeoutError("x")), \
                patch.object(graph.llm, "writer_llm") as writer:
            result = graph.run("列出三位未流失客户")
        self.assertEqual(result["basis"], "upstream_unavailable")
        writer.assert_not_called()

        router = Mock()
        router.bind_tools.side_effect = ConnectionError("x")
        result, _, _ = self._run(router)
        self.assertEqual(result["basis"], "upstream_unavailable")

    def test_second_decision_timeout_after_successful_query_keeps_cards(self):
        """E02-T03：已有成功数据时降级 → 数据卡片完整保留，写手不再调用。"""
        state = initial_state("fixture", "查一下 C000001")
        state["tool_results"] = [ok_record()]
        state["messages"] = [AIMessage(content="", tool_calls=[
            {"name": "list_customers", "args": {"limit": 1}, "id": "x"}])]
        router = Mock()
        router.bind_tools.return_value = router
        router.invoke.side_effect = TimeoutError("x")
        with patch.object(graph.llm, "router_llm", return_value=router):
            decided = graph.node_agent(state)
        self.assertTrue(decided["upstream_failure"])
        self.assertEqual(graph.route_after_agent({**state, **decided}), "synthesize")

        merged = {**state, **decided}
        synth = graph.node_synthesis(merged)
        self.assertIn("模型解读暂不可用", synth["synthesis_warning"])
        with patch.object(graph.llm, "writer_llm") as writer:
            out = graph.node_verify({**merged, **synth})
        writer.assert_not_called()
        self.assertEqual(out["answer"]["entities"][0]["customer_id"], "C000001")
        self.assertEqual(out["answer_basis"], "system_data")
        self.assertIn("模型解读暂不可用", out["answer"]["warning"]["text"])

    def test_forced_tool_state_does_not_loop_after_failure(self):
        """E02-T04/T05：旧消息含 tool_calls、强制标志已置位时失败 → 不再进强制分支。"""
        state = initial_state("fixture", "查一下 C000001 的风险详情")
        state["force_tool"] = True
        state["force_write_tool"] = "propose_update_work_order"
        state["rounds"] = 1
        state["messages"] = [AIMessage(content="", tool_calls=[
            {"name": "list_customers", "args": {"limit": 1}, "id": "x"}])]
        router = Mock()
        router.bind_tools.return_value = router
        router.invoke.side_effect = TimeoutError("x")
        with patch.object(graph.llm, "router_llm", return_value=router):
            out = graph.node_agent(state)
        self.assertTrue(out["upstream_failure"])
        self.assertEqual(graph.route_after_agent({**state, **out}), "synthesize")
        self.assertEqual(out["messages"][0].content, "")

    def test_deadline_exhausted_does_not_call_model(self):
        """E02-T06：预算已耗尽 → 不调用模型。"""
        state = initial_state("fixture", "查 C000001")
        state["deadline"] = time.monotonic() - 1
        with patch.object(graph.llm, "router_llm") as factory:
            out = graph.node_agent(state)
        factory.assert_not_called()
        self.assertNotIn("upstream_failure", out)
        self.assertEqual(graph.route_after_agent({**state, **out}), "synthesize")

    def test_unknown_programming_error_still_propagates(self):
        """E02-T10：未知错误保持可观测，不被包装成正常降级。"""
        router = Mock()
        router.bind_tools.return_value = router
        router.invoke.side_effect = KeyError("boom")
        with patch.object(graph.llm, "router_llm", return_value=router):
            with self.assertRaises(KeyError):
                graph.run("列出三位未流失客户")

    def test_request_contextvars_are_restored_after_failure(self):
        """E02-T09：异常退出不得留下跨请求的 deadline 污染。"""
        router = Mock()
        router.bind_tools.return_value = router
        router.invoke.side_effect = TimeoutError("x")
        with patch.object(graph.llm, "router_llm", return_value=router), \
                patch.object(graph.llm, "writer_llm"):
            graph.run("列出三位未流失客户")
        self.assertIsNone(tools._request_deadline.get())

class DegradeContract(unittest.TestCase):
    def test_degraded_answer_has_no_business_facts_or_pending(self):
        """E02-T05：无数据降级 → 无客户/工单事实、无新 pending。"""
        state = initial_state("fixture", "查 C000001")
        state["upstream_failure"] = True
        state["upstream_error"] = {"code": "upstream_timeout", "retryable": True,
                                   "message": "模型服务响应超时，本轮未能完成解读。"}
        synth = graph.node_synthesis(state)
        out = graph.node_verify({**state, **synth})
        self.assertEqual(out["answer"]["entities"], [])
        self.assertEqual(out["answer"]["facts"], [])
        self.assertNotIn("pending_action", out["answer"])
        self.assertEqual(out["answer_basis"], "upstream_unavailable")
        self.assertIsNone(out.get("pending_action"))


if __name__ == "__main__":
    unittest.main()
