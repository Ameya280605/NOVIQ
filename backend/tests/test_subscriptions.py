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
)


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
            plan = db.query(Plan).one()
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
            plan = db.query(Plan).one()
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
            self.assertEqual(db.query(Plan).count(), 0)
            self.assertEqual(db.query(Subscription).count(), 0)
