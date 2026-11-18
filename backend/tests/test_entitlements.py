import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "entitlement-test-signing-key"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import (
    Base,
    Plan,
    PlanEntitlement,
    Subscription,
    Tenant,
    User,
)
from backend.app.utils.jwt import create_access_token


class EntitlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        with self.session_factory() as db:
            plan = Plan(code="entitlement-test", name="Entitlement Test")
            db.add(plan)
            db.flush()
            db.add(
                PlanEntitlement(
                    plan_id=plan.id,
                    feature_key="assistant",
                    enabled=True,
                )
            )
            tenant, owner = self._create_user(db, "Tenant One", "owner")
            member = User(
                tenant_id=tenant.id,
                name="Member",
                email="entitlement-member@example.test",
                password_hash="not-a-real-hash",
                role="member",
            )
            viewer = User(
                tenant_id=tenant.id,
                name="Viewer",
                email="entitlement-viewer@example.test",
                password_hash="not-a-real-hash",
                role="viewer",
            )
            other_tenant, other_owner = self._create_user(
                db, "Tenant Two", "owner"
            )
            db.add_all([member, viewer])
            db.flush()
            start = datetime.now(timezone.utc)
            db.add_all(
                [
                    Subscription(
                        tenant_id=tenant.id,
                        plan_id=plan.id,
                        current_period_start=start,
                        current_period_end=start + timedelta(days=30),
                    ),
                    Subscription(
                        tenant_id=other_tenant.id,
                        plan_id=plan.id,
                        current_period_start=start,
                        current_period_end=start + timedelta(days=30),
                    ),
                ]
            )
            db.commit()
            self.tenant_id = tenant.id
            self.owner_id = owner.id
            self.member_id = member.id
            self.viewer_id = viewer.id
            self.other_tenant_id = other_tenant.id
            self.other_owner_id = other_owner.id

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _create_user(db, tenant_name: str, role: str):
        tenant = Tenant(name=tenant_name)
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id,
            name=f"{role} user",
            email=f"{tenant_name}-{role}@example.test",
            password_hash="not-a-real-hash",
            role=role,
        )
        db.add(user)
        db.flush()
        return tenant, user

    @staticmethod
    def _headers(user_id: int, tenant_id: int, role: str):
        token = create_access_token(
            {"sub": str(user_id), "tenant_id": tenant_id, "role": role}
        )
        return {"Authorization": f"Bearer {token}"}

    def test_enabled_entitlement_and_database_roles_are_required(self):
        member_response = self.client.post(
            "/assistant/query",
            headers=self._headers(self.member_id, self.tenant_id, "member"),
            json={"question": "List project status"},
        )
        self.assertEqual(member_response.status_code, 200, member_response.text)

        forged_viewer = self.client.post(
            "/assistant/query",
            headers=self._headers(self.viewer_id, self.tenant_id, "member"),
            json={"question": "List project status"},
        )
        self.assertEqual(forged_viewer.status_code, 403)

    def test_disabled_entitlement_and_cross_tenant_identity_are_rejected(self):
        analytics = self.client.get(
            "/dashboard/overview",
            headers=self._headers(self.owner_id, self.tenant_id, "owner"),
        )
        self.assertEqual(analytics.status_code, 403)
        self.assertIn("advanced_analytics", analytics.json()["detail"])

        forged_tenant = self.client.post(
            "/assistant/query",
            headers=self._headers(
                self.owner_id, self.other_tenant_id, "owner"
            ),
            json={"question": "List project status"},
        )
        self.assertEqual(forged_tenant.status_code, 401)

    def test_missing_or_inactive_subscription_is_rejected(self):
        with self.session_factory() as db:
            subscription = (
                db.query(Subscription)
                .filter(Subscription.tenant_id == self.tenant_id)
                .one()
            )
            subscription.status = "canceled"
            db.commit()

        response = self.client.post(
            "/assistant/query",
            headers=self._headers(self.owner_id, self.tenant_id, "owner"),
            json={"question": "List project status"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("active subscription", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
