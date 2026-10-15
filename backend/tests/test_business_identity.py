"""业务口径及历史身份兼容，在隔离数据库验证。"""
import unittest
from unittest.mock import patch
from app.services import assignee_identity, note_service, model_service
from app.models.user import User
from app.models.work_order import WorkOrder
import test_agent_order_safety as fixtures


class BusinessIdentityTests(unittest.TestCase):
    setUp = fixtures.OrderSafetyTests.setUp
    tearDown = fixtures.OrderSafetyTests.tearDown

    def test_customer_absence_and_model_unavailable_have_distinct_statuses(self):
        from app.routers import customers
        with patch.object(customers, 'get_customer_detail', return_value=None):
            for suffix in ('', '/explanation', '/suggested-note'):
                missing = self.client.get('/api/customers/C999999' + suffix)
                self.assertEqual(missing.status_code, 404)
                self.assertNotIn('模型', missing.json()['detail'])
                offline = self.client.get('/api/customers/C000001' + suffix)
                self.assertEqual(offline.status_code, 503)
                self.assertNotIn('不存在', offline.json()['detail'])

    def test_individual_scenario_note_uses_actual_customer_balance_and_explicit_cost_basis(self):
        result = note_service.build_reason(prob=.89371, balance=195028.2, risk_level='CRITICAL', value_tier='HIGH')
        self.assertAlmostEqual(result['components']['expected_recoverable'], .89371 * 195028.2 * .3, places=2)
        self.assertEqual(result['metric_basis']['value_source'], 'this_customer_balance')
        self.assertIn('概率×该客户余额', result['note'])
        self.assertIn('单次成本=', result['note'])
        self.assertNotIn('金额按假设客单价', result['note'])

    def test_unique_legacy_aliases_and_new_identity_are_one_filter_without_similar_name_matches(self):
        with self.sessions() as db:
            db.add_all([User(username='owner', display_name='赵敏', role='staff', is_active=1, password_hash='fixture'),
                WorkOrder(customer_id='C000001', customer_name='fixture', status='cancelled', assignee='owner'),
                WorkOrder(customer_id='C000002', customer_name='fixture', status='cancelled', assignee='赵敏'),
                WorkOrder(customer_id='C000002', customer_name='fixture', status='cancelled', assignee='赵敏敏')])
            db.commit()
        response = self.client.get('/api/work-orders', params={'assignee':'owner'}).json()
        self.assertEqual(response['total'], 2)
        self.assertEqual(self.client.get('/api/work-orders', params={'assignee':'赵'}).json()['total'], 0)
        self.assertEqual(self.client.get('/api/work-orders/stats').json()['assignees'].count('owner'), 1)

    def test_duplicate_names_do_not_grant_staff_legacy_ownership_or_allow_new_name_assignment(self):
        with self.sessions() as db:
            for username in ('first', 'second'):
                db.add(User(username=username, display_name='同名', role='staff', is_active=1, password_hash='fixture'))
            db.add(WorkOrder(id=99, customer_id='C000001', customer_name='fixture', status='cancelled', assignee='同名'))
            db.add(WorkOrder(id=100, customer_id='C000002', customer_name='fixture', status='cancelled', assignee='同名', assignee_username='first'))
            db.commit()
            self.user = db.query(User).filter_by(username='second').one()
        self.assertEqual(self.client.get('/api/work-orders').json()['total'], 0)
        self.assertEqual(self.client.get('/api/work-orders/99').status_code, 404)
        self.assertEqual(self.client.get('/api/work-orders/100').status_code, 404)
        with self.sessions() as db:
            with self.assertRaisesRegex(Exception, '同名'):
                assignee_identity.resolve(db, '同名')

    def test_preview_backfill_preserves_original_name_and_handles_ambiguity_and_rename(self):
        with self.sessions() as db:
            db.add_all([User(username='unique', display_name='旧名', role='staff', is_active=1, password_hash='fixture'),
                User(username='a', display_name='同名', role='staff', is_active=1, password_hash='fixture'),
                User(username='b', display_name='同名', role='staff', is_active=1, password_hash='fixture')])
            for oid, owner in ((99,'旧名'), (100,'同名'), (101,'未知历史人员')):
                db.add(WorkOrder(id=oid, customer_id='C000001', customer_name='fixture', status='cancelled', assignee=owner))
            db.commit()
            plan = assignee_identity.preview(db)
            self.assertEqual({entry['original']:entry['status'] for entry in plan}['同名'], 'ambiguous')
            self.assertEqual({entry['original']:entry['status'] for entry in plan}['未知历史人员'], 'unmatched')
            assignee_identity.apply_preview(db, plan, self.user)
            db.commit()
            order = db.get(WorkOrder, 99)
            self.assertEqual((order.assignee,order.assignee_username,order.assignee_name_snapshot), ('旧名','unique','旧名'))
            user = db.query(User).filter_by(username='unique').one()
            user.display_name = '新名'
            db.commit()
            self.assertTrue(assignee_identity.matches(order, user, db))
            self.assertIsNone(db.get(WorkOrder,100).assignee_username)

    def test_new_owner_name_is_normalized_to_id_and_inactive_or_viewer_cannot_receive_orders(self):
        with self.sessions() as db:
            db.add_all([User(username='inactive', display_name='停用', role='staff', is_active=0, password_hash='fixture'),
                        User(username='analyst', display_name='分析', role='viewer', is_active=1, password_hash='fixture')])
            db.commit()
        response = self.client.post('/api/work-orders', json={'customer_id':'C000001', 'assignee':self.user.display_name})
        self.assertEqual(response.status_code, 201, response.text)
        with self.sessions() as db:
            order = db.get(WorkOrder, response.json()['id'])
            self.assertEqual(order.assignee, 'tester')
            self.assertEqual(order.assignee_username, 'tester')
            self.assertEqual(order.assignee_name_snapshot, self.user.display_name)
            for owner in ('inactive', 'analyst'):
                with self.subTest(owner=owner), self.assertRaises(Exception):
                    assignee_identity.resolve(db, owner)

    def test_model_auc_first_is_separate_from_published_model_and_version(self):
        metadata = {'results': {name: {'accuracy':.8,'precision':.8,'recall':.8,'f1_score':.8,
                    'auc':auc,'cv_auc_mean':cv} for name,auc,cv in [('A',.7,.9),('B',.99,.8)]},
                    'best_model':'A','version':'fixture'}
        from types import SimpleNamespace
        service = model_service.ModelService(None)
        service._meta = metadata
        service._manifest = SimpleNamespace(best_model='A', revision='fixture-version', status='published')
        with patch.object(service, '_ensure_meta', return_value=True), patch.object(service, '_engine_metrics_for_best', return_value=None):
            result = service.get_model_comparison()
        self.assertEqual(result['models'][0]['model_name'], 'B')
        self.assertEqual(result['best_model'], 'A')
        self.assertEqual(result['model_version'], 'fixture-version')
