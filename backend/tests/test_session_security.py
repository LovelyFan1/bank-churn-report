"""服务端会话回归：只访问fixture临时SQLite及临时用户。"""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.schema import CreateTable

from app.config import settings
from app.database import get_db
from app.middleware.auth import AuthMiddleware
from app.models.user import User, AuthSession, RevokedToken
from app.models.work_order import WorkOrder
from app.routers import auth as auth_router
from app.services import auth_deps, auth_service
import test_agent_order_safety as order_fixtures


class SessionSecurityTests(unittest.TestCase):
    def setUp(self):
        order_fixtures.OrderSafetyTests.setUp(self)
        self.client.app.dependency_overrides.pop(auth_deps.current_user)
        with self.sessions() as db:
            db.query(User).filter_by(username="tester").update({
                "password_hash": auth_service.hash_password("Initial-Test-1234")})
            db.commit()
        self.auth_patches = [patch.object(settings, "AUTH_ENABLED", True),
                             patch.object(settings, "AUTH_REQUIRE_TOTP", False),
                             patch.object(settings, "AUTH_IDLE_MINUTES", 1),
                             patch.object(settings, "AUTH_TOKEN_TTL_MINUTES", 120),
                             patch.object(auth_service, "_jwt_secret", return_value=b"isolated-session-key")]
        for item in self.auth_patches:
            item.start()
        self.clock_patcher = patch.object(auth_service.time, "time", return_value=2_000_000_000)
        self.clock = self.clock_patcher.start()

    def tearDown(self):
        self.clock_patcher.stop()
        for item in reversed(self.auth_patches):
            item.stop()
        order_fixtures.OrderSafetyTests.tearDown(self)

    def login(self, password="Initial-Test-1234"):
        response = self.client.post("/api/auth/login", json={"username": "tester", "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["token"]}

    def second_worker(self):
        app = FastAPI()
        app.include_router(auth_router.router)
        app.add_middleware(AuthMiddleware)
        app.dependency_overrides[get_db] = self.client.app.dependency_overrides[get_db]
        return TestClient(app)

    def test_idle_boundary_and_activity_cannot_revive_expired_session(self):
        headers = self.login()
        self.clock.return_value += 59
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        self.clock.return_value += 1
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/activity", headers=headers).status_code, 401)

    def test_passive_reads_do_not_extend_idle_session(self):
        headers = self.login()
        start = self.clock.return_value
        for elapsed in (15, 30, 45):
            self.clock.return_value = start + elapsed
            self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        self.clock.return_value = start + 60
        with self.second_worker() as second:
            self.assertEqual(second.get("/api/auth/me", headers=headers).status_code, 401)

    def test_user_activity_extends_only_its_session(self):
        first, other = self.login(), self.login()
        self.clock.return_value += 45
        response = self.client.post("/api/auth/activity", headers=first)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["idle_remaining"], 60)
        self.clock.return_value += 15
        with self.second_worker() as second:
            self.assertEqual(second.get("/api/auth/me", headers=first).status_code, 200)
            self.assertEqual(second.get("/api/auth/me", headers=other).status_code, 401)
        self.clock.return_value += 45
        self.assertEqual(self.client.get("/api/auth/me", headers=first).status_code, 401)

    def test_password_change_invalidates_all_old_sessions(self):
        first, other = self.login(), self.login()
        response = self.client.post("/api/auth/change-password", headers=first, json={
            "old_password": "Initial-Test-1234", "new_password": "Changed-Test-5678"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["reauth_required"])
        for headers in (first, other):
            self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 401)
            with self.second_worker() as second:
                self.assertEqual(second.get("/api/auth/me", headers=headers).status_code, 401)
        self.login("Changed-Test-5678")

    def test_absolute_expiry_is_not_extended_by_activity(self):
        with patch.object(settings, "AUTH_TOKEN_TTL_MINUTES", 1), \
             patch.object(settings, "AUTH_IDLE_MINUTES", 5):
            headers = self.login()
            self.clock.return_value += 59
            self.assertEqual(self.client.post("/api/auth/activity", headers=headers).status_code, 200)
            self.clock.return_value += 1
            self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 401)

    def test_unregistered_legacy_token_requires_login(self):
        token = auth_service.make_token({"sub": "tester", "scope": "full"})
        self.assertEqual(self.client.get("/api/auth/me", headers={
            "Authorization": "Bearer " + token}).status_code, 401)

    def test_failed_password_change_does_not_revoke_valid_session(self):
        headers = self.login()
        response = self.client.post("/api/auth/change-password", headers=headers, json={
            "old_password": "Initial-Test-1234", "new_password": "abcdefgh"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)

    def test_me_reports_server_policy_and_remaining_idle(self):
        with patch.object(settings, "AUTH_IDLE_MINUTES", 3):
            headers = self.login()
            self.clock.return_value += 41
            data = self.client.get("/api/auth/me", headers=headers).json()
            self.assertEqual(data["idle_timeout"], 180)
            self.assertEqual(data["idle_remaining"], 139)

    def test_login_and_password_change_roll_back_when_audit_fails(self):
        headers = self.login()
        with self.sessions() as db:
            count = db.query(AuthSession).count()
        with patch.object(auth_deps, "write_audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.post("/api/auth/change-password", headers=headers, json={
                    "old_password": "Initial-Test-1234", "new_password": "Changed-Test-5678"})
            with self.assertRaises(RuntimeError):
                self.login()
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        with self.sessions() as db:
            self.assertEqual(db.query(AuthSession).count(), count)
        self.login()

    def test_activity_cannot_revive_concurrently_revoked_session(self):
        from fastapi import HTTPException
        headers = self.login()
        payload = auth_service.decode_token(headers["Authorization"].split(" ", 1)[1])
        original = auth_service.validate_session
        def revoked_after_validation(db, body):
            user = original(db, body)
            with self.sessions() as second:
                second.add(RevokedToken(jti=body["jti"], expires_at=body["exp"]))
                second.commit()
            return user
        self.clock.return_value += 20
        with self.sessions() as db, patch.object(auth_service, "validate_session", side_effect=revoked_after_validation):
            with self.assertRaises(HTTPException) as caught:
                auth_service.record_activity(db, payload)
            self.assertEqual(caught.exception.status_code, 401)
        with self.sessions() as db:
            self.assertEqual(db.get(AuthSession, payload["jti"]).last_activity_at, 2_000_000_000)

    def test_separate_process_reads_shared_session_and_idle_expiry(self):
        headers = self.login()
        script = '''
import json,sys
from fastapi import HTTPException
from app.database import SessionLocal
from app.services import auth_service as auth
data=json.load(sys.stdin)
auth.time.time=lambda:data['now']
with SessionLocal() as db:
    try:
        auth.validate_session(db,auth.decode_token(data['token']))
        print(200)
    except HTTPException as exc:
        print(exc.status_code)
    except auth.TokenError:
        print(401)
'''
        for elapsed, expected in ((59, 200), (60, 401)):
            result = subprocess.run([sys.executable, "-c", script],
                input=json.dumps({"token": headers["Authorization"].split(" ", 1)[1],
                                  "now": 2_000_000_000 + elapsed}),
                env=dict(os.environ, DATABASE_URL=str(self.engine.url), AUTH_SECRET_KEY="isolated-session-key",
                         AUTH_IDLE_MINUTES=str(settings.AUTH_IDLE_MINUTES),
                         AUTH_TOKEN_TTL_MINUTES=str(settings.AUTH_TOKEN_TTL_MINUTES)),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(expected))

    def test_totp_ticket_is_invalidated_by_password_change(self):
        with self.sessions() as db:
            user = db.query(User).filter_by(username="tester").one()
            user.totp_secret = auth_service.gen_totp_secret()
            user.totp_enabled = 1
            secret = user.totp_secret
            db.commit()
        full = self.login()
        with patch.object(settings, "AUTH_REQUIRE_TOTP", True):
            response = self.client.post("/api/auth/login", json={
                "username": "tester", "password": "Initial-Test-1234"})
            ticket = response.json()["ticket"]
            self.assertEqual(self.client.get("/api/auth/me", headers={
                "Authorization": "Bearer " + ticket}).status_code, 401)
            valid = self.client.post("/api/auth/login/totp", json={
                "ticket": ticket, "code": auth_service.totp_now(secret)})
            self.assertEqual(valid.status_code, 200, valid.text)
            self.assertEqual(self.client.post("/api/auth/change-password", headers=full, json={
                "old_password": "Initial-Test-1234", "new_password": "Changed-Test-5678"}).status_code, 200)
            self.assertEqual(self.client.post("/api/auth/login/totp", json={
                "ticket": ticket, "code": auth_service.totp_now(secret)}).status_code, 401)
            with patch.object(settings, "AUTH_DEMO_SHOW_TOTP", True):
                self.assertEqual(self.client.post("/api/auth/demo/totp", json={"ticket": ticket}).status_code, 401)

    def test_session_migration_is_idempotent_and_preserves_existing_rows(self):
        from pathlib import Path
        from app.migrate import run_migrations
        legacy = create_engine(f"sqlite:///{Path(self.tmp.name) / 'legacy.db'}")
        try:
            with legacy.begin() as conn:
                for table, missing in ((User.__table__, "auth_version"), (WorkOrder.__table__, "created_by")):
                    ddl = str(CreateTable(table).compile(dialect=legacy.dialect))
                    conn.execute(text("\n".join(line for line in ddl.splitlines() if missing not in line)))
                conn.execute(text("INSERT INTO users (id,username,password_hash) VALUES (1,'existing','preserve-hash')"))
                conn.execute(text("INSERT INTO work_orders (id,customer_id,status) VALUES (1,'legacy-customer','completed')"))
            changes = run_migrations(legacy)
            self.assertEqual(set(changes), {"auth_sessions", "users.auth_version", "work_orders.created_by"})
            self.assertEqual(run_migrations(legacy), [])
            self.assertIn("auth_sessions", inspect(legacy).get_table_names())
            with legacy.connect() as conn:
                self.assertEqual(tuple(conn.execute(text("SELECT username,password_hash,auth_version FROM users")).one()),
                                 ("existing", "preserve-hash", 0))
                self.assertEqual(tuple(conn.execute(text("SELECT customer_id,status FROM work_orders")).one()),
                                 ("legacy-customer", "completed"))
        finally:
            legacy.dispose()


if __name__ == "__main__":
    unittest.main()
