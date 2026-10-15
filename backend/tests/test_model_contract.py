"""仅使用临时发布目录、合成小模型及隔离API验证模型契约。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import joblib
from app.celery_tasks import predict, train
from app.services import risk_scoring, model_service
import test_agent_order_safety as fixtures


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        version = self.root / 'versions' / 'fixture'
        version.mkdir(parents=True)
        joblib.dump({'identity': 'A'}, version / 'a.joblib')
        joblib.dump({'identity': 'B-old'}, self.root / 'b.joblib')
        self.meta = {'version': 'fixture', 'best_model': 'A', 'results': {
            'A': {'cv_auc_mean': .9, 'auc': .7}, 'B': {'cv_auc_mean': .8, 'auc': .99}},
            'model_files': {'A': 'versions/fixture/a.joblib', 'B': 'versions/fixture/b.joblib'}}
        joblib.dump({'identity': 'B'}, version / 'b.joblib')
        (self.root / 'meta.json').write_text(json.dumps(self.meta), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_batch_and_single_use_published_cv_selection_and_paths(self):
        with patch.object(predict, 'MODEL_DIR', self.root), patch.object(model_service, 'MODEL_DIR', self.root):
            batch, name = predict._load_best_model()
            single = model_service.ModelService(None).get_best_model()
        self.assertEqual(name, 'A')
        self.assertEqual(batch, single)
        self.assertEqual(batch['identity'], 'A')

    def test_missing_published_file_never_uses_root_legacy_model(self):
        (self.root / 'versions' / 'fixture' / 'a.joblib').unlink()
        joblib.dump({'identity': 'A-old'}, self.root / 'a.joblib')
        with patch.object(predict, 'MODEL_DIR', self.root):
            self.assertIsNone(predict._load_best_model()[0])
        with self.assertRaises(ValueError):
            risk_scoring.model_artifact(self.meta, 'missing', self.root)

    def test_failed_publication_preserves_previous_manifest(self):
        before = (self.root / 'meta.json').read_bytes()
        with patch.object(train, 'MODEL_DIR', self.root), patch.object(train.joblib, 'dump', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                train._save_results_to_disk({'A': object()}, {'A': {'cv_auc_mean': .8}})
        self.assertEqual((self.root / 'meta.json').read_bytes(), before)


class PredictionSchemaTests(unittest.TestCase):
    setUp = fixtures.OrderSafetyTests.setUp
    tearDown = fixtures.OrderSafetyTests.tearDown

    def test_invalid_features_are_rejected_before_loading_model(self):
        valid = dict(credit_score=650, age=39, tenure=5, balance=76000,
            num_products=2, has_credit_card=1, is_active_member=1, estimated_salary=100000,
            satisfaction_score=3, points_earned=500, geography='France', gender='Male')
        invalid = [{}, dict(valid, age='abc'), dict(valid, balance=-1), dict(valid, age=True),
                   dict(valid, geography='Mars'), dict(valid, gender='Unknown'), dict(valid, has_credit_card=2)]
        invalid.append({key: value for key, value in valid.items() if key != 'balance'})
        with patch.object(model_service.ModelService, 'predict_single', return_value={'probability': .2}) as predict_one:
            for body in invalid:
                with self.subTest(body=body):
                    self.assertEqual(self.client.post('/api/model/predict', json=body).status_code, 422)
            predict_one.assert_not_called()

    def test_nonfinite_values_are_rejected_and_valid_boundaries_preserve_feature_order(self):
        from app.schemas.prediction import PredictionFeatures, feature_vector
        from pydantic import ValidationError
        valid = dict(credit_score=350, age=18, tenure=0, balance=0, num_products=1,
                     has_credit_card=0, is_active_member=0, estimated_salary=0,
                     satisfaction_score=1, points_earned=0, geography='Spain', gender='Female')
        self.assertEqual(feature_vector(valid).tolist(), [[350,18,0,0,1,0,0,0,1,0,2,1]])
        for value in (float('nan'), float('inf'), float('-inf'), True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PredictionFeatures.model_validate(dict(valid, balance=value))
        for value in ('NaN', 'Infinity', '-Infinity'):
            raw = json.dumps(valid).replace('"balance": 0', '"balance": ' + value)
            self.assertEqual(self.client.post('/api/model/predict', content=raw,
                headers={'content-type':'application/json'}).status_code, 422)


class ShapContractTests(unittest.TestCase):
    def test_real_binary_forests_boosters_and_multiple_rows_are_consistent(self):
        import numpy as np
        from sklearn.datasets import make_classification
        from sklearn.ensemble import RandomForestClassifier
        from xgboost import XGBClassifier
        from lightgbm import LGBMClassifier
        from app.services.shap_contract import explain
        X, y = make_classification(n_samples=80, n_features=12, n_informative=4, random_state=11)
        for model in (RandomForestClassifier(n_estimators=5, max_depth=3, random_state=1),
                      XGBClassifier(n_estimators=5, max_depth=2, n_jobs=1, random_state=1),
                      LGBMClassifier(n_estimators=5, max_depth=2, n_jobs=1, verbosity=-1)):
            with self.subTest(model=type(model).__name__):
                model.fit(X, y)
                single, batch = explain(model, X[:1]), explain(model, X[:3])
                self.assertEqual(batch['values'].shape, (3, 12))
                np.testing.assert_allclose(single['values'][0], batch['values'][0], atol=1e-5)
                self.assertTrue(batch['additivity_checked'])
                records = [(float(model.predict_proba(X[:1])[0,1]), {}, X[i]) for i in range(3)]
                predict._enrich_with_shap(model, records)
                self.assertTrue(all(row[1].get('shap_additivity_checked') for row in records))
                self.assertTrue(all(len(row[1]['shap_contributions']) == 12 for row in records))

    def test_positive_class_comes_from_model_classes_and_supports_list_form(self):
        import numpy as np
        from types import SimpleNamespace
        from app.services.shap_contract import normalize
        model = SimpleNamespace(classes_=np.array([1, 0]))
        values, base = normalize([np.ones((2,12)), np.zeros((2,12))], [.3,.7], model, (2,12))
        self.assertTrue((values == 1).all())
        self.assertEqual(base, .3)


class EngineStateTests(unittest.TestCase):
    def test_actual_missing_corrupt_and_incompatible_reload_then_recovery(self):
        import numpy as np
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier
        from app.services.data_loader import prepare_features, FEATURE_NAMES
        from contextlib import ExitStack
        rows = []
        for i in range(40):
            rows.append(dict(customer_id=f'C{i:06}', surname='fixture', id=i,
                credit_score=600 + i, age=20 + i, tenure=i % 10, balance=i * 3000,
                num_products=1 + i % 4, has_credit_card=i % 2, is_active_member=(i//2) % 2,
                estimated_salary=10000 + i * 1000, satisfaction_score=1 + i % 5,
                points_earned=i * 10, geography='France', gender='Male', exited=i % 2))
        df = pd.DataFrame(rows)
        X, y, _ = prepare_features(df)
        model = RandomForestClassifier(n_estimators=4, random_state=1).fit(X, y)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            joblib.dump(model, root / 'forest.joblib')
            meta = {'results': {'Forest': {'auc':.9, 'cv_auc_mean':.8}}, 'feature_names':FEATURE_NAMES,
                    'dataset_fingerprint':risk_scoring.dataset_fingerprint(df)}
            def publish(value):
                (root / 'meta.json').write_text(json.dumps(value), encoding='utf-8')
            publish(meta)
            stack.enter_context(patch.object(risk_scoring, 'MODEL_DIR', root))
            stack.enter_context(patch.object(risk_scoring, '_MODEL_VERSION_FILE', root / '.model_version'))
            stack.enter_context(patch.object(risk_scoring, '_engine_cache', risk_scoring._empty_engine_cache()))
            stack.enter_context(patch.object(risk_scoring, '_best_model_cache', dict(name=None, model=None, ts=0, version=None)))
            stack.enter_context(patch.object(risk_scoring, '_scored_cache', dict(data=None, name=None, ts=0)))
            stack.enter_context(patch.object(risk_scoring, 'get_cached_customer_df', return_value=df))
            stack.enter_context(patch('app.services.data_loader._read_cache_version', return_value='fixture-data'))
            self.assertIsNotNone(risk_scoring._ensure_engine(None))
            self.assertTrue(risk_scoring.get_risk_info()['calibrated'])
            for invalid in ('missing_file', 'corrupt_meta', 'wrong_fingerprint'):
                if invalid == 'missing_file':
                    (root / 'forest.joblib').unlink()
                elif invalid == 'corrupt_meta':
                    (root / 'meta.json').write_text('{broken', encoding='utf-8')
                else:
                    publish(dict(meta, dataset_fingerprint='wrong'))
                self.assertIsNone(risk_scoring._ensure_engine(None), invalid)
                self.assertIsNone(risk_scoring.get_risk_info()['thresholds'], invalid)
                joblib.dump(model, root / 'forest.joblib')
                publish(meta)
                self.assertIsNotNone(risk_scoring._ensure_engine(None), invalid)

            rolling=dict(meta,dataset_fingerprint='previous-training-cohort')
            rolling['results']={'Forest':{'auc':.9,'threshold_validation':[{'threshold':.4,'tp':10,'fp':1,'fn':3}]}}
            publish(rolling)
            engine=risk_scoring._ensure_engine(None)
            self.assertIsNotNone(engine)
            self.assertFalse(engine['training_dataset_matches'])
            self.assertEqual(engine['decision_threshold'],.4)
            self.assertEqual(risk_scoring.evaluate_at_decision_threshold(),{})
            bad=df.copy();bad.loc[0,'geography']='Unknown'
            with self.assertRaises(ValueError): prepare_features(bad)

    def test_failed_reload_clears_thresholds_and_can_recover(self):
        import numpy as np
        previous = risk_scoring._empty_engine_cache()
        previous.update(raw=np.array([.8]), thresholds={'critical': .7}, model_version='old',
                        status='ready', name='old')
        with patch.object(risk_scoring, '_engine_cache', previous), patch.object(risk_scoring, '_scored_cache', {'data': ['old']}), \
             patch.object(risk_scoring, '_build_engine', return_value=None):
            self.assertIsNone(risk_scoring._ensure_engine(None))
            state = risk_scoring.get_risk_info()
            self.assertFalse(state['calibrated'])
            self.assertIsNone(state['thresholds'])
            self.assertIsNone(state['model_version'])
            self.assertEqual(state['status'], 'unavailable')
            self.assertIsNone(risk_scoring._scored_cache['data'])
            with patch.object(risk_scoring, '_build_engine', return_value={'raw': np.array([.4])}):
                self.assertEqual(risk_scoring._ensure_engine(None)['status'], 'ready')


class TaskSubmissionTests(unittest.TestCase):
    setUp = fixtures.OrderSafetyTests.setUp
    tearDown = fixtures.OrderSafetyTests.tearDown

    def test_post_train_concurrency_response_loss_and_broker_redelivery_do_not_duplicate(self):
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        from app.services.task_submission import track_submission
        from app.models.task_submission import TaskSubmission
        self.user.role = 'admin'
        with patch.object(train.train_all_models_task, 'apply_async') as enqueue:
            def send(_):
                response = self.client.post('/api/model/train', headers={'Idempotency-Key': 'same-training-request'})
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()['task_id']
            with ThreadPoolExecutor(max_workers=2) as pool:
                ids = list(pool.map(send, range(2)))
            self.assertEqual(ids[0], ids[1])
            self.assertEqual(send(0), ids[0])
            self.assertEqual(enqueue.call_count, 1)
        calls = []
        @track_submission
        def fake_task(self):
            calls.append('ran')
            return {'status': 'completed', 'value': 1}
        worker = SimpleNamespace(request=SimpleNamespace(id=ids[0]))
        self.assertEqual(fake_task(worker)['status'], 'completed')
        self.assertEqual(fake_task(worker)['status'], 'duplicate_delivery')
        self.assertEqual(calls, ['ran'])
        state = self.client.get('/api/tasks/' + ids[0]).json()
        self.assertEqual(state['status'], 'SUCCESS')
        self.assertEqual(state['result']['value'], 1)

    def test_batch_fixed_version_retries_and_payload_conflict(self):
        from types import SimpleNamespace
        from app.routers import models
        self.user.role = 'admin'
        manifest = SimpleNamespace(meta={'version':'fixture'}, revision='v1', status='published')
        engine = dict(model_version='v1', thresholds={'critical':.7,'high':.3,'medium':.1}, customer_version='data1')
        with patch.object(models.model_manifest, 'resolve', return_value=manifest), \
             patch.object(risk_scoring, '_ensure_engine', return_value=engine), \
             patch.object(predict.batch_score_task, 'apply_async') as enqueue:
            headers = {'Idempotency-Key':'same-batch-request'}
            first = self.client.post('/api/model/batch-score', json={'top_n':25}, headers=headers)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(enqueue.call_args.kwargs['kwargs']['model_snapshot']['model_version'], 'v1')
            with patch.object(models.model_manifest, 'resolve', side_effect=AssertionError('重试不得读取新模型')):
                second = self.client.post('/api/model/batch-score', json={'top_n':25}, headers=headers)
            self.assertEqual(second.json()['task_id'], first.json()['task_id'])
            conflict = self.client.post('/api/model/batch-score', json={'top_n':50}, headers=headers)
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(enqueue.call_count, 1)
            self.assertEqual(self.client.get('/api/model/batch-score').status_code, 405)

    def test_broker_failure_retry_reuses_task_id_and_nonadmin_cannot_submit(self):
        from app.models.task_submission import TaskSubmission
        self.user.role = 'admin'
        headers = {'Idempotency-Key':'temporary-broker-failure'}
        with patch.object(train.train_all_models_task, 'apply_async', side_effect=OSError('fixture queue unavailable')):
            self.assertEqual(self.client.post('/api/model/train', headers=headers).status_code, 503)
        with self.sessions() as db:
            task_id = db.query(TaskSubmission).one().task_id
        with patch.object(train.train_all_models_task, 'apply_async'):
            retry = self.client.post('/api/model/train', headers=headers)
            self.assertEqual(retry.json()['task_id'], task_id)
        self.user.role = 'staff'
        self.assertEqual(self.client.post('/api/model/train', headers=headers).status_code, 403)
