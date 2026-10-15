"""认证分类与审计权限，只在会话 fixture 数据库执行。"""
import unittest
from unittest.mock import patch
from app.models.user import AuditLog, User
from app.config import settings
from app.services import auth_service
import test_session_security as session_fixtures


class AuditMethodTests(unittest.TestCase):
    setUp = session_fixtures.SessionSecurityTests.setUp
    tearDown = session_fixtures.SessionSecurityTests.tearDown
    login = session_fixtures.SessionSecurityTests.login

    def test_password_and_totp_success_failure_have_independent_method_and_result(self):
        with self.sessions() as db:
            db.query(User).filter_by(username='tester').update({'role':'admin'})
            db.commit()
        headers = self.login()
        self.client.post('/api/auth/login', json={'username':'missing','password':'wrong'})
        with self.sessions() as db:
            db.query(User).filter_by(username='tester').update({'totp_enabled':1, 'totp_secret':'fixture'})
            db.commit()
        with patch.object(settings, 'AUTH_REQUIRE_TOTP', True):
            ticket = self.client.post('/api/auth/login', json={'username':'tester','password':'Initial-Test-1234'}).json()['ticket']
        with patch.object(auth_service, 'totp_verify', return_value=False):
            self.assertEqual(self.client.post('/api/auth/login/totp', json={'ticket':ticket,'code':'123456'}).status_code,401)
        with patch.object(auth_service, 'totp_verify', return_value=True):
            self.assertEqual(self.client.post('/api/auth/login/totp', json={'ticket':ticket,'code':'654321'}).status_code,200)
        for method, success, expected in [('totp','true',1),('totp','false',1),('password','false',1)]:
            data = self.client.get('/api/auth/audit', params={'action':'login','auth_method':method,'success':success},headers=headers).json()
            self.assertEqual(data['total'],expected)
            self.assertTrue(all(r['auth_method']==method for r in data['items']))
        with self.sessions() as db:
            failed = db.query(AuditLog).filter_by(target='missing').one()
            self.assertEqual(failed.username,'')
            all_rows = str([(r.detail,r.message) for r in db.query(AuditLog)])
            self.assertNotIn('123456',all_rows); self.assertNotIn('654321',all_rows)

    def test_legacy_method_unknown_is_not_invented_and_nonadmin_cannot_read(self):
        with self.sessions() as db:
            db.add_all([AuditLog(action='login',success=1), AuditLog(action='login_totp',success=0)])
            db.commit()
        headers = self.login()
        self.assertEqual(self.client.get('/api/auth/audit',headers=headers).status_code,403)
        with self.sessions() as db:
            db.query(User).filter_by(username='tester').update({'role':'admin'})
            db.commit()
        data = self.client.get('/api/auth/audit',params={'auth_method':'totp'},headers=headers).json()
        self.assertEqual(data['total'],1)
        self.assertEqual(data['items'][0]['auth_method'],'totp')

if __name__ == '__main__': unittest.main()
