"""名单容量、数量与连续查询的确定性回归，不调用外部模型。"""
import unittest
from unittest.mock import patch

from app.agent import context, tools, quantities, list_queries, graph, verify, answer_builder
import test_agent_order_safety as fixtures


class BatchCapacityTests(unittest.TestCase):
    def test_all_displayed_ids_remain_addressable_after_detail_cache_truncation(self):
        ids = [f"C{i:06}" for i in range(1, 51)]
        records = [{"tool": "list_customers", "ok": True, "data": {
            "items": [{"customer_id": cid} for cid in ids], "next_offset": 50}}]
        ctx = context.normalize(context.merge(context.empty(), context.extract(records, None)))
        self.assertEqual(ctx["displayed_customer_ids"], ids)
        self.assertLessEqual(len(ctx["customers"]), 20)
        self.assertIn(ids[24], context.allowed_customer_ids(ctx, "第二十五位", []))
        self.assertIn("25. " + ids[24], context.render(ctx))

    def test_batch_plan_conserves_twenty_to_fifty_targets_and_duplicates(self):
        def response(path, **_):
            cid = path.rsplit("/", 1)[-1]
            if cid == "suggested-note":
                return {"note": "fixture"}
            return {"customer_id": cid, "exited": 1 if cid == "C000003" else 0}

        for size in (20, 21, 25, 50):
            ids = [f"C{i:06}" for i in range(1, size + 1)]
            with self.subTest(size=size), patch.object(tools, "_get", side_effect=response):
                proposal = tools.propose_create_work_orders_batch(ids + [ids[0]])
            self.assertLessEqual(len(proposal["items"]), 20)
            actual = [it["customer_id"] for it in proposal["items"]]
            actual += [it["customer_id"] for it in proposal["failed"]]
            actual += proposal["remaining_customer_ids"]
            self.assertEqual(set(actual), set(ids))
            self.assertEqual(len(actual), len(ids))
            self.assertEqual(proposal["requested_customer_ids"], ids)


class QuantityAndCursorTests(unittest.TestCase):
    def test_read_only_suffix_does_not_replace_customer_primary_with_workorders(self):
        records = [{'tool': 'list_customers', 'ok': True, 'data': {'items': [
            {'customer_id': 'C000001', 'surname': '甲', 'risk_level': 'HIGH', 'probability': .8}],
            'returned': 1, 'matched_total': 90}}]
        question = '按风险资产评分列出一位未流失且没有在途工单的客户，不创建工单'
        result = graph.node_verify({'question': question, 'tool_results': records,
                                    'headline': '推荐一位客户', 'insights': []})
        self.assertTrue(result['verify_passed'])
        self.assertEqual(result['answer']['kind'], 'customer_list')
        self.assertEqual(result['answer']['entities'][0]['customer_id'], 'C000001')
        details = [{'tool': 'get_customer_risk', 'ok': True, 'data': records[0]['data']['items'][0]}]
        self.assertEqual(answer_builder.build(details, '查看第一位的风险详情，不创建工单')['kind'], 'customer_detail')

    def test_explicit_geography_is_enforced_even_when_llm_omits_it(self):
        from langchain_core.messages import AIMessage
        state = {'question': '列出二十五位德国未流失客户，不创建工单', 'messages': [
            AIMessage(content='', tool_calls=[{'name':'list_customers', 'args':{'limit':5}, 'id':'read'}])]}
        with patch.object(tools, 'call_tool', return_value={'ok':True,'result':{'items':[]}}) as query:
            graph.node_tools(state)
        self.assertEqual(query.call_args.args[1]['geography'], 'Germany')
        self.assertEqual(query.call_args.args[1]['limit'], 25)

    def test_continue_same_sort_keeps_exclusions_and_does_not_call_llm(self):
        previous = {'sort_by':'expected_value','limit':3,'next_offset':15,
                    'seen_customer_ids':['C000001'],'model_version':'v1'}
        with patch.object(tools, 'call_tool', return_value={'ok':True,'result':{'items':[]}}) as query, \
                patch.object(graph,'get_graph') as llm:
            graph.run('继续列出十二位，按风险资产评分，不创建工单', context={'list_query':previous})
        llm.assert_not_called()
        self.assertEqual(query.call_args.args[1]['exclude_ids'], ['C000001'])
        self.assertEqual(query.call_args.args[1]['offset'], 15)

    def test_ordinal_reference_cannot_select_another_known_customer(self):
        ids = [f'C{i:06}' for i in range(1,26)]
        state={'question':'查看第25位的风险详情，不创建工单',
               'context_in':{'displayed_customer_ids':ids}}
        self.assertIsNone(graph._scope_error(state,'get_customer_risk',{'customer_id':ids[24]},[]))
        self.assertIsNotNone(graph._scope_error(state,'get_customer_risk',{'customer_id':ids[0]},[]))

    def test_chinese_quantities_ordinals_and_amounts_are_distinct(self):
        for number, chinese in ((1, '一'), (2, '两'), (10, '十'), (12, '十二'), (20, '二十'), (21, '二十一'), (25, '二十五'), (30, '三十'), (50, '五十')):
            with self.subTest(number=number):
                self.assertEqual(quantities.quantity('另外' + chinese + '位'), number)
                self.assertEqual(quantities.quantity(f'另外{number}位'), number)
                self.assertEqual(quantities.ordinal('第' + chinese + '位'), number)
                self.assertIsNone(quantities.quantity('第' + chinese + '位'))
        self.assertIsNone(quantities.quantity('十二万元'))
        for invalid in ('十几位', '五十一位', '100位', '-2位', '2.5位', '零位'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                quantities.quantity(invalid)

    def test_long_continuations_keep_filter_and_cursor_until_actual_change(self):
        previous = dict(sort_by='expected_value', geography='France', next_offset=3,
                        seen_customer_ids=['C000001'], model_version='v1')
        for phrase in ('另外十二位挽回价值最高的客户，排除刚才三位，只查询，不创建工单',
                       '再来十二位值得投入的客户', '下一批'):
            # 第二句在原任务worth_only=True时仅复述口径。
            source = dict(previous, worth_only=True)
            args, changed = list_queries.continuation(source, phrase)
            self.assertFalse(changed)
            self.assertEqual(args['offset'], 3)
            self.assertEqual(args['exclude_ids'], ['C000001'])
        args, changed = list_queries.continuation(previous, '另外十二位德国客户')
        self.assertTrue(changed)
        self.assertEqual(args['geography'], 'Germany')
        self.assertEqual(args['offset'], 0)
        self.assertNotIn('exclude_ids', args)
        self.assertTrue(list_queries.continuation(previous, '下一批按概率排序')[1])

    def test_deterministic_three_batches_preserve_sizes_and_exclude_previous(self):
        ctx = context.merge(context.empty(), context.extract([{'tool': 'list_customers', 'ok': True,
            'args': {'sort_by': 'expected_value', 'limit': 3}, 'data': {'items': [
                {'customer_id': f'C{i:06}'} for i in range(1, 4)], 'next_offset': 3, 'model_version': 'v1'}}], None))
        observed = []
        def query(name, args):
            self.assertEqual(name, 'list_customers')
            self.assertEqual(args['limit'], 12)
            excluded = set(args.get('exclude_ids') or [])
            ids = [f'C{i:06}' for i in range(1, 80) if f'C{i:06}' not in excluded][:12]
            observed.extend(ids)
            return {'ok': True, 'result': {'items': [{'customer_id': cid} for cid in ids],
                'returned': 12, 'matched_total': 79, 'next_offset': args['offset'] + 12, 'model_version': 'v1'}}
        with patch.object(tools, 'call_tool', side_effect=query), patch.object(graph, 'get_graph') as llm:
            for _ in range(3):
                ctx = graph.run('另外十二位挽回价值最高的客户，只查询，不创建工单', context=ctx)['context']
            llm.assert_not_called()
        self.assertEqual(len(observed), 36)
        self.assertEqual(len(set(observed)), 36)
        self.assertNotIn('C000001', observed)

    def test_old_model_cursor_is_rejected(self):
        with patch.object(tools, '_get', return_value={'items': [], 'risk': {'model_version': 'new'}}):
            with self.assertRaisesRegex(ValueError, '模型快照'):
                tools.list_customers(expected_version='old')

    def test_primary_counts_and_ordinals_have_sources_not_global_number_pool(self):
        records = [{'tool': 'list_customers', 'ok': True, 'data': {'items': [
            {'customer_id': f'C{i:06}', 'probability': .8} for i in range(1, 4)], 'returned': 3, 'matched_total':90}},
            {'tool': 'list_work_orders', 'ok': True, 'data': {'items': [{}, {}], 'returned': 2}}]
        for correct in ('推荐三位客户', '本次返回3人', '本次返回3 位客户', '符合条件共有90 人，本次返回3 位', '第一位C000001概率80%', '返回人数3', '本页展示三位，符合条件共有九十位'):
            self.assertTrue(verify.verify(correct, records, question='推荐客户名单')[0], correct)
        for incorrect in ('推荐四位客户', '推荐2人', '第四位客户', '符合条件共有三位'):
            self.assertFalse(verify.verify(incorrect, records, question='推荐客户名单')[0], incorrect)


class SessionLifecycleTests(unittest.TestCase):
    setUp = fixtures.OrderSafetyTests.setUp
    tearDown = fixtures.OrderSafetyTests.tearDown
    ask = fixtures.OrderSafetyTests.ask
    confirm = fixtures.OrderSafetyTests.confirm

    def test_retracted_confirmation_cannot_execute_and_is_idempotent(self):
        pending = self.ask()['pending_action']
        for _ in range(2):
            self.assertEqual(self.client.post('/api/agent/retract', json={
                'confirmation_tokens': [pending['confirmation_token']]}).status_code, 200)
        self.assertEqual(self.confirm(pending).status_code, 409)

    def test_clear_invalidates_current_context_and_tokens_without_touching_other_user(self):
        from app.agent.order_safety import AgentSession
        import time
        pending = self.ask()['pending_action']
        with self.sessions() as db:
            db.add(AgentSession(username='other', session_id='default', context='{"customers":[]}',
                                version=7, expires_at=time.time() + 1000))
            db.commit()
        response = self.client.post('/api/agent/session/default/clear', json={
            'confirmation_tokens': [pending['confirmation_token']]})
        self.assertEqual(response.status_code, 200)
        state = self.client.get('/api/agent/session/default').json()
        self.assertTrue(state['expired'])
        self.assertEqual(state['context']['customers'], [])
        self.assertEqual(self.confirm(pending).status_code, 409)
        with self.sessions() as db:
            self.assertEqual(db.get(AgentSession, ('other', 'default')).version, 7)

    def test_other_user_cannot_retract_owner_token(self):
        from app.models.user import User
        pending = self.ask()['pending_action']
        original = self.user
        self.user = User(username='other', role='manager', is_active=1)
        self.assertEqual(self.client.post('/api/agent/retract', json={
            'confirmation_tokens': [pending['confirmation_token']]}).status_code, 200)
        self.user = original
        self.assertEqual(self.confirm(pending).status_code, 200)
