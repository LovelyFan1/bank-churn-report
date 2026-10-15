"""结果声明/独立复核、ROC 压缩；全部使用独立 fixture。"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import numpy as np
from app.models.work_order import WorkOrder
from app.models.user import User
from app.services.cost_benefit_service import CostBenefitService
from app.services.curve_sampling import simplify
import test_agent_order_safety as fixtures


class ObservationTests(unittest.TestCase):
    setUp = fixtures.OrderSafetyTests.setUp
    tearDown = fixtures.OrderSafetyTests.tearDown

    def test_demo_unknown_and_manual_are_not_merged_or_claimed_actual_profit(self):
        with self.sessions() as db:
            db.query(WorkOrder).update({'result_source':'unknown'})
            for i,source in enumerate(['demo','manual','manual']):
                db.add(WorkOrder(customer_id='C000001',status='completed',result='retained',result_source=source,
                    actual_cost=10 if i==1 else None,actual_benefit=20 if i==1 else None))
            db.commit()
            with patch('app.services.risk_scoring._ensure_engine',return_value=None):
                data=CostBenefitService(db).get_retention_summary()
            self.assertEqual(data['total_completed'],2)
            self.assertEqual(data['excluded_demo'],1); self.assertEqual(data['historical_unknown'],3)
            self.assertEqual(data['verified_count'],0); self.assertIsNone(data['actual_cost'])
            self.assertFalse(data['is_causal_effect']); self.assertTrue(data['roi_is_scenario'])

    def test_observation_requires_ended_window_evidence_and_independent_reviewer(self):
        with self.sessions() as db:
            db.add(WorkOrder(id=999,customer_id='C000001',status='completed',result='retained',result_source='manual',assignee_username='tester',result_recorded_by='tester'))
            db.commit()
        self.assertEqual(self.client.post('/api/work-orders/999/verify').status_code,403)
        with self.sessions() as db:
            reviewer=User(username='reviewer',display_name='复核',role='manager',is_active=1,password_hash='fixture');db.add(reviewer);db.commit();self.user=reviewer
        self.assertEqual(self.client.post('/api/work-orders/999/verify').status_code,422)
        now=datetime.now(timezone.utc)
        response=self.client.put('/api/work-orders/999',json={'observation_start':(now-timedelta(days=30)).isoformat(),'observation_end':(now+timedelta(days=1)).isoformat(),'result_evidence':'隔离回访编号'})
        self.assertEqual(response.status_code,200,response.text)
        with self.sessions() as db:
            independent=User(username='independent',display_name='复核2',role='manager',is_active=1,password_hash='fixture');db.add(independent);db.commit();self.user=independent
        self.assertEqual(self.client.post('/api/work-orders/999/verify').status_code,422)
        self.client.put('/api/work-orders/999',json={'observation_end':now.isoformat()})
        # The evidence editor cannot attest their own revision; use another reviewer.
        with self.sessions() as db:
            self.user=db.query(User).filter_by(username='reviewer').one()
        self.assertEqual(self.client.post('/api/work-orders/999/verify').status_code,200)
        self.assertEqual(self.client.put('/api/work-orders/999',json={'note':'普通备注'}).json()['verification_state'],'verified')
        self.assertEqual(self.client.put('/api/work-orders/999',json={'actual_cost':10}).json()['verification_state'],'unverified')

    def test_invalid_window_and_nonfinite_cost_rejected(self):
        now=datetime.now(timezone.utc).isoformat()
        response=self.client.put('/api/work-orders/8',json={'observation_start':now,'observation_end':now})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.put('/api/work-orders/8',json={'actual_cost':-1}).status_code,422)


class CurveTests(unittest.TestCase):
    def test_stratified_sample_is_stable_exact_and_keeps_rare_groups(self):
        import pandas as pd
        from app.services.sampling import bounded_sample
        df=pd.DataFrame({'cluster_id':[0]*20000+[1]*2,'exited':[0]*10000+[1]*10000+[0,1]})
        first=bounded_sample(df,15000);second=bounded_sample(df,15000)
        self.assertEqual(len(first),15000);self.assertTrue(first.equals(second))
        self.assertEqual(len(first.groupby(['cluster_id','exited'])),4)

    def test_stair_curve_area_endpoint_and_coordinates_preserved_with_bounded_error(self):
        x=np.linspace(0,1,20000);y=np.sqrt(x)
        result=simplify({'fpr':x.tolist(),'tpr':y.tolist(),'thresholds':(1-x).tolist()})
        self.assertEqual((result['fpr'][0],result['tpr'][-1]),(0,1))
        self.assertLess(len(result['fpr']),500)
        self.assertLessEqual(abs(np.trapz(y,x)-np.trapz(result['tpr'],result['fpr'])),.0005)
        predicted=np.interp(x,result['fpr'],result['tpr'])
        self.assertLessEqual(float(np.max(np.abs(y-predicted))),.0005)
        self.assertEqual(simplify({'fpr':[0,0,0,1],'tpr':[0,.3,1,1]})['fpr'],[0,0,1])

if __name__ == '__main__': unittest.main()
