import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "subscription-task-test-signing-key"

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from celery.exceptions import Retry
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.celery_app import celery_app
from backend.app.models import Base, Plan, Subscription, Tenant
from backend.app.tasks import subscriptions as subscription_tasks


class SubscriptionTaskTests(unittest.TestCase):
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
            plan = Plan(code="task-plan", name="Task Plan")
            db.add(plan)
            db.flush()
            expired_tenant = Tenant(name="Expired Tenant")
            current_tenant = Tenant(name="Current Tenant")
            db.add_all([expired_tenant, current_tenant])
            db.flush()
            now = datetime.now(timezone.utc)
            db.add_all(
                [
                    Subscription(
                        tenant_id=expired_tenant.id,
                        plan_id=plan.id,
                        status="active",
                        current_period_start=now - timedelta(days=31),
                        current_period_end=now - timedelta(days=1),
                        cancel_at_period_end=True,
                    ),
                    Subscription(
                        tenant_id=current_tenant.id,
                        plan_id=plan.id,
                        status="active",
                        current_period_start=now,
                        current_period_end=now + timedelta(days=30),
                    ),
                ]
            )
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    def test_expiry_is_tenant_scoped_and_duplicate_execution_is_safe(self):
        with patch.object(subscription_tasks, "SessionLocal", self.session_factory):
            first = subscription_tasks.expire_subscriptions.apply(throw=True)
            duplicate = subscription_tasks.expire_subscriptions.apply(throw=True)

        self.assertEqual(first.result, {"expired_count": 1})
        self.assertEqual(duplicate.result, {"expired_count": 0})
        with self.session_factory() as db:
            statuses = {
                row.status
                for row in db.query(Subscription).all()
            }
            self.assertEqual(statuses, {"canceled", "active"})

    def test_expiry_retries_operational_database_failures(self):
        temporary_failure = OperationalError("temporary", None, None)
        with patch.object(
            subscription_tasks,
            "SessionLocal",
            side_effect=[temporary_failure, self.session_factory],
        ):
            with self.assertRaises(Retry):
                subscription_tasks.expire_subscriptions.apply(throw=True)

    def test_expiry_task_is_scheduled_every_minute(self):
        entry = celery_app.conf.beat_schedule[
            "schedule-subscription-expiry-every-minute"
        ]
        self.assertEqual(
            entry["task"],
            "backend.app.tasks.subscriptions.expire_subscriptions",
        )
        self.assertEqual(entry["schedule"]._orig_minute, "*")


if __name__ == "__main__":
    unittest.main()
