"""E03 回归 —— 可信历史事实与严格核验。

固定复现数据（虚构客户，禁止依赖正式数据）：
    上一轮 A：C000001，probability=0.8，balance=275100.3125，expected_value=220080.25
    本轮   B：C000002，probability=0.2，balance=250000，expected_value=50000
"""
import time
import unittest

from app.agent import context, graph, verify


A = "C000001"
B = "C000002"


def fact(cid=A, metric="expected_value", value=220080.25, unit="yuan",
         observed_at=None, source="get_customer_risk", version="fixture-v1"):
    return {"entity_type": "customer", "entity_id": cid, "metric": metric,
            "value": value, "unit": unit,
            "observed_at": time.time() if observed_at is None else observed_at,
            "source": source, "model_version": version}


def customer_row(cid, probability, balance, expected_value):
    return {"customer_id": cid, "surname": "Fixture", "probability": probability,
            "balance": balance, "expected_value": expected_value,
            "risk_level": "HIGH", "value_tier": "LOW", "exited": 0,
            "has_active_order": False}


def tool_record(row, tool="get_customer_risk"):
    return {"tool": tool, "args": {"customer_id": row["customer_id"]},
            "ok": True, "data": row}


class HistoricalReference(unittest.TestCase):
    def test_history_value_passes_with_entity_and_metric(self):
        """E03-T01：同会话可信历史来源下，引用上次的风险资产评分应通过。"""
        ok, errors = verify.verify("刚才 C000001 的风险资产评分为 220080.25 元", [],
                                   extra_sources=[fact()])
        self.assertTrue(ok, errors)

    def test_probability_percent_and_fraction_both_work(self):
        """E03-T02：80% 与 0.8 都应通过，单位换算不放宽。"""
        history = [fact(metric="probability", value=0.8, unit="fraction")]
        for text in ("刚才 C000001 的流失概率是 80%", "刚才 C000001 的流失概率是 0.8"):
            with self.subTest(text=text):
                ok, errors = verify.verify(text, [], extra_sources=history)
                self.assertTrue(ok, errors)

    def test_history_value_cannot_be_reassigned_or_metric_swapped(self):
        """E03-T03：把 A 的评分说成 B 的评分、把评分说成余额，都要拒绝。"""
        history = [fact(), fact(metric="balance", value=275100.3125)]
        current = [tool_record(customer_row(B, 0.2, 250000, 50000))]
        for text, code in (("C000002 的风险资产评分为 220080.25 元", "metric_value_mismatch"),
                           ("C000002 的余额是 220080.25 元", "metric_value_mismatch")):
            with self.subTest(text=text):
                ok, errors = verify.verify(text, current, extra_sources=history)
                self.assertFalse(ok)
                self.assertIn(code, errors)

    def test_unit_errors_are_still_rejected(self):
        """E03-T04：单位错误不放宽。"""
        history = [fact(metric="probability", value=0.8, unit="fraction")]
        current = [tool_record(customer_row(B, 0.2, 250000, 50000))]
        for text in ("C000002 的流失概率是 50 元", "C000002 的风险资产评分为 50%"):
            with self.subTest(text=text):
                ok, errors = verify.verify(text, current, extra_sources=history)
                self.assertFalse(ok)
                self.assertIn("unit_mismatch", errors)

    def test_plain_number_list_is_not_a_whitelist(self):
        """E03-T05：只传浮点列表/字典不得成为核验依据。"""
        for bad in ([220080.25, 0.8], {"probability": 0.8}, "220080.25"):
            with self.subTest(bad=bad):
                ok, errors = verify.verify("刚才 C000001 的风险资产评分为 220080.25 元",
                                           [], extra_sources=bad)
                self.assertFalse(ok)
                self.assertIn("unknown_entity", errors)

    def test_current_value_wins_over_history(self):
        """E03-T08：同一实体本轮新值优先，旧值不能替新值过检。"""
        history = [fact()]
        current = [tool_record(customer_row(B, 0.2, 250000, 50000))]
        draft = "C000002 的风险资产评分为 50000 元"
        ok, errors = verify.verify(draft, current, extra_sources=history)
        self.assertTrue(ok, errors)
        ok, errors = verify.verify("C000002 的风险资产评分为 220080.25 元", current,
                                   extra_sources=history)
        self.assertFalse(ok)
        self.assertIn("metric_value_mismatch", errors)


class HistoryStore(unittest.TestCase):
    def test_client_cannot_inject_facts_even_on_first_turn(self):
        """E03-T06：客户端伪造的事实、版本、时间一律丢弃。"""
        raw = {"customers": [{"id": A, "name": "x", "prob": 0.99, "balance": 1}],
               "facts": [fact(value=999999.0)]}
        ctx = context.normalize(raw)          # 默认 trusted=False
        self.assertEqual(ctx["facts"], [])
        ctx = context.normalize(raw, trusted=True)
        self.assertEqual(len(ctx["facts"]), 1)
        self.assertEqual(ctx["facts"][0]["value"], 999999.0)   # 服务端写出的值原样保留

    def test_expired_and_future_facts_are_dropped(self):
        """E03-T07：过期与未来时间戳不被接受（客户端时间不能延长有效期）。"""
        now = time.time()
        stale = fact(observed_at=now - context.FACT_TTL_SECONDS - 5)
        future = fact(observed_at=now + 60)
        kept = fact(observed_at=now - 60)
        ctx = context.normalize({"facts": [stale, future, kept]}, trusted=True)
        self.assertEqual([f["value"] for f in ctx["facts"]], [220080.25])
        self.assertEqual(ctx["facts"][0]["observed_at"], kept["observed_at"])

    def test_model_version_is_kept_when_available(self):
        """E03-T09：有版本就保留，没有就不编造。"""
        ctx = context.normalize({"facts": [fact(version="model-2026-10"),
                                           fact(metric="balance", value=1.0, version=None)]},
                                trusted=True)
        versions = {f["metric"]: f["model_version"] for f in ctx["facts"]}
        self.assertEqual(versions["expected_value"], "model-2026-10")
        self.assertIsNone(versions["balance"])

    def test_ttl_caps_and_multi_answer_trimming(self):
        """E03-T13：容量、回答批次与清空边界。"""
        now = time.time()
        many = [fact(cid=f"C{i:06d}", value=float(i))
                for i in range(1, 260)]
        for item in many:
            item["observed_at"] = now
        ctx = context.normalize({"facts": many}, trusted=True)
        self.assertLessEqual(len(ctx["facts"]), context.MAX_FACTS)

        # 11 个不同回答批次 → 只保留最近 10 个
        batches = []
        for i in range(11):
            item = fact(cid=f"C{i:06d}", value=float(i))
            item["observed_at"] = now - (11 - i) * 30
            batches.append(item)
        merged = context.merge(context.empty(), {"facts": batches})
        self.assertEqual(len({int(f["observed_at"]) for f in merged["facts"]}), 10)
        self.assertNotIn(batches[0]["entity_id"],
                         {f["entity_id"] for f in merged["facts"]})

    def test_facts_do_not_change_displayed_list_or_write_targets(self):
        """E03-T10：历史事实不得把 A 插回当前 B 的名单或改变写目标。"""
        listing = {"tool": "list_customers", "args": {"limit": 1}, "ok": True,
                   "data": {"items": [customer_row(B, 0.2, 250000, 50000)],
                            "next_offset": 1, "model_version": "fixture-v1"}}
        prev = context.merge(context.empty(), context.extract([listing], None))
        prev["facts"] = [fact()]              # 上一轮留下的 A 的历史值
        ctx = context.merge(prev, context.extract([], None))
        self.assertEqual(ctx["displayed_customer_ids"], [B])
        self.assertEqual([c["id"] for c in ctx["customers"]], [B])
        allowed = context.allowed_customer_ids(ctx, "给第一位建单", [])
        self.assertEqual(allowed, {B})
        # 显式写出编号仍属新话题（既有协议）；历史事实不参与授权集合
        self.assertNotIn(A, allowed)

    def test_failures_and_pending_are_never_fact_sources(self):
        """E03-T12：失败记录、待确认动作与模型文本不能成为事实来源。"""
        failed = {"tool": "get_customer_risk", "args": {"customer_id": A}, "ok": False,
                  "data": {"error": "x", "error_code": "TIMEOUT", "retryable": True}}
        self.assertEqual(context._facts_from_record(failed, time.time()), [])
        extracted = context.extract([failed], {"action": "create_work_order",
                                               "payload": {"customer_id": A,
                                                           "expected_value_snapshot": 999.0}})
        self.assertEqual(extracted["facts"], [])

    def test_history_counts_cannot_pass_current_claims(self):
        """E03-T11：历史人数不能替当前主结果过检。"""
        rows = [customer_row(f"C{i:06d}", 0.2, 1000, 200) for i in range(1, 4)]
        current = {"tool": "list_customers", "args": {"limit": 3}, "ok": True,
                   "data": {"items": rows, "returned": 3, "matched_total": 3,
                            "is_complete": True}}
        ok, errors = verify.verify("符合条件共 20 人", [current], extra_sources=[fact()])
        self.assertFalse(ok)
        self.assertTrue({"returned_count_mismatch", "metric_value_mismatch"} & set(errors))


class HistoryInGraph(unittest.TestCase):
    def test_rejected_history_claims_never_reach_public_fields(self):
        """E03-T14：被拒绝的解读在 headline/insights/text 中整体消失。"""
        current = {"tool": "get_customer_risk", "args": {"customer_id": B}, "ok": True,
                   "data": customer_row(B, 0.2, 250000, 50000)}
        state = {
            "question": "C000002 的情况",
            "tool_results": [current],
            "history_facts": [fact()],
            "headline": "C000002 的风险资产评分为 220080.25 元",
            "insights": ["C000002 的余额是 220080.25 元"],
            "direct_answer": "",
        }
        out = graph.node_verify(state)
        self.assertFalse(out["verify_passed"])
        answer = out["answer"]
        self.assertNotIn("220080.25", str(answer.get("headline")))
        self.assertNotIn("220080.25", " ".join(answer.get("insights") or []))
        self.assertEqual(answer["entities"][0]["customer_id"], B)

    def test_history_citation_only_when_referenced(self):
        """E03-T12：只有真正被引用的历史值才出现在 citations。"""
        state = {"history_facts": [fact()], "tool_results": []}
        self.assertEqual(graph._history_citations(state, "C000001 的风险资产评分为 220080.25 元"),
                         [f"history://{A}?metric=expected_value"])
        self.assertEqual(graph._history_citations(state, "今天天气不错"), [])

    def test_writer_receives_trimmed_history(self):
        """E03-T11：写手能拿到裁剪后的历史事实，但拿不到整份对话。"""
        block = graph._history_block({"history_facts": [
            {**fact(), "observed_at_iso": "2026-10-23 02:00:00"}]})
        self.assertIn(A, block)
        self.assertIn("上次查询", block)
        self.assertIn("不能", block)          # 明确禁止当成最新值
        self.assertEqual(graph._history_block({"history_facts": []}), "")


if __name__ == "__main__":
    unittest.main()
