import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "subscription-test-signing-key"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
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


class SubscriptionTests(unittest.TestCase):
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
            free_plan = Plan(
                code="free",
                name="Free",
                max_users=3,
                max_projects=3,
                max_tasks=100,
                max_documents=100,
                max_storage_bytes=1024**3,
            )
            db.add(free_plan)
            db.flush()
            db.add(
                PlanEntitlement(
                    plan_id=free_plan.id,
                    feature_key="document_upload",
                    enabled=True,
                )
            )
            db.add(Plan(code="starter", name="Starter", is_active=True))
            db.add(Plan(code="legacy", name="Legacy", is_active=False))
            db.commit()

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

    @staticmethod
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @staticmethod
    def _headers(user_id: int, tenant_id: int, role: str) -> dict[str, str]:
        token = create_access_token(
            {"sub": str(user_id), "tenant_id": tenant_id, "role": role}
        )
        return {"Authorization": f"Bearer {token}"}

    def _create_user(self, db, tenant_name: str, role: str):
        tenant = Tenant(name=tenant_name)
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id,
            name=f"{role} user",
            email=f"{tenant_name}-{role}@example.test",
            password_hash="not-a-real-password-hash",
            role=role,
        )
        db.add(user)
        db.flush()
        return tenant, user

    def test_lists_active_plans_and_retrieves_only_authenticated_tenant_subscription(
        self,
    ) -> None:
        with self.session_factory() as db:
            tenant, owner = self._create_user(db, "Tenant One", "owner")
            other_tenant, other_owner = self._create_user(
                db, "Tenant Two", "owner"
            )
            free_plan = db.query(Plan).filter(Plan.code == "free").one()
            start = datetime.now(timezone.utc)
            db.add(
                Subscription(
                    tenant_id=tenant.id,
                    plan_id=free_plan.id,
                    current_period_start=start,
                    current_period_end=start + timedelta(days=30),
                )
            )
            db.commit()
            owner_headers = self._headers(owner.id, tenant.id, "owner")
            other_headers = self._headers(
                other_owner.id, other_tenant.id, "owner"
            )

        plans = self.client.get("/plans", headers=owner_headers)
        self.assertEqual(plans.status_code, 200, plans.text)
        self.assertEqual(
            {plan["code"] for plan in plans.json()},
            {"free", "starter"},
        )

        subscription = self.client.get(
            "/subscription", headers=owner_headers
        )
        self.assertEqual(subscription.status_code, 200, subscription.text)
        self.assertEqual(subscription.json()["tenant_id"], tenant.id)
        self.assertEqual(subscription.json()["plan"]["code"], "free")

        missing_other_subscription = self.client.get(
            "/subscription", headers=other_headers
        )
        self.assertEqual(missing_other_subscription.status_code, 404)

    def test_subscription_management_uses_database_role_and_validates_plan(self):
        with self.session_factory() as db:
            tenant, owner = self._create_user(db, "Managed Tenant", "owner")
            member = User(
                tenant_id=tenant.id,
                name="member user",
                email="managed-member@example.test",
                password_hash="not-a-real-password-hash",
                role="member",
            )
            db.add(member)
            db.flush()
            free_plan = db.query(Plan).filter(Plan.code == "free").one()
            start = datetime.now(timezone.utc)
            db.add(
                Subscription(
                    tenant_id=tenant.id,
                    plan_id=free_plan.id,
                    current_period_start=start,
                    current_period_end=start + timedelta(days=30),
                )
            )
            db.commit()
            owner_headers = self._headers(owner.id, tenant.id, "owner")
            forged_member_headers = self._headers(member.id, tenant.id, "owner")

        invalid_plan = self.client.patch(
            "/subscription",
            headers=forged_member_headers,
            json={"plan_code": "does-not-exist"},
        )
        self.assertEqual(invalid_plan.status_code, 403)

        invalid_plan = self.client.patch(
            "/subscription",
            headers=owner_headers,
            json={"plan_code": "legacy"},
        )
        self.assertEqual(invalid_plan.status_code, 400)

        changed = self.client.patch(
            "/subscription",
            headers=owner_headers,
            json={"plan_code": "starter"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["plan"]["code"], "starter")

        canceled = self.client.post(
            "/subscription/cancel", headers=owner_headers
        )
        self.assertEqual(canceled.status_code, 200, canceled.text)
        self.assertTrue(canceled.json()["subscription"]["cancel_at_period_end"])

    def test_subscription_rejects_tampered_tenant_claim_and_reports_missing_subscription(
        self,
    ) -> None:
        with self.session_factory() as db:
            tenant, owner = self._create_user(db, "Claim Tenant", "owner")
            db.commit()
            valid_headers = self._headers(owner.id, tenant.id, "owner")
            tampered_headers = self._headers(owner.id, tenant.id + 100, "owner")

        missing = self.client.get("/subscription", headers=valid_headers)
        self.assertEqual(missing.status_code, 404)

        tampered = self.client.get("/subscription", headers=tampered_headers)
        self.assertEqual(tampered.status_code, 401)

    def test_registration_creates_tenant_scoped_free_subscription(self) -> None:
        response = self.client.post(
            "/users/register",
            json={
                "name": "Owner",
                "email": "subscription-owner@example.test",
                "password": "password123",
                "company_name": "Subscription Tenant",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        with self.session_factory() as db:
            tenant = db.query(Tenant).one()
            subscription = db.query(Subscription).one()
            self.assertEqual(subscription.tenant_id, tenant.id)
            self.assertEqual(subscription.plan.code, "free")
            self.assertEqual(subscription.status, "active")
            self.assertGreater(
                subscription.current_period_end,
                subscription.current_period_start,
            )

    def test_tenant_can_have_only_one_subscription(self) -> None:
        with self.session_factory() as db:
            tenant = Tenant(name="Unique Subscription Tenant")
            plan = db.query(Plan).filter(Plan.code == "free").one()
            db.add(tenant)
            db.flush()
            start = datetime.now(timezone.utc)
            db.add(
                Subscription(
                    tenant_id=tenant.id,
                    plan_id=plan.id,
                    current_period_start=start,
                    current_period_end=start + timedelta(days=30),
                )
            )
            db.commit()
            db.add(
                Subscription(
                    tenant_id=tenant.id,
                    plan_id=plan.id,
                    current_period_start=start,
                    current_period_end=start + timedelta(days=60),
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()

    def test_registration_does_not_recreate_missing_free_plan(self) -> None:
        with self.session_factory() as db:
            plan = db.query(Plan).filter(Plan.code == "free").one()
            db.delete(plan.entitlements[0])
            db.delete(plan)
            db.commit()

        response = self.client.post(
            "/users/register",
            json={
                "name": "Owner",
                "email": "missing-plan@example.test",
                "password": "password123",
                "company_name": "Missing Plan Tenant",
            },
        )

        self.assertEqual(response.status_code, 500)
        with self.session_factory() as db:
            self.assertEqual(db.query(Plan).count(), 2)
            self.assertIsNone(
                db.query(Plan).filter(Plan.code == "free").one_or_none()
            )
            self.assertEqual(db.query(Subscription).count(), 0)
