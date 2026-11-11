import importlib.util
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, Notification, Tenant, User


class NotificationSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    def test_notification_columns_defaults_relationships_and_indexes(self) -> None:
        with Session(self.engine) as db:
            tenant = Tenant(name="Tenant One")
            recipient = User(
                tenant=tenant,
                name="Recipient",
                email="recipient@example.test",
                password_hash="hash",
            )
            notification = Notification(
                tenant=tenant,
                recipient=recipient,
                event_type="task_assigned",
                title="Task assigned",
                message="A task was assigned to you.",
                entity_type="task",
                entity_id=42,
            )
            db.add(notification)
            db.commit()
            db.refresh(notification)

            self.assertFalse(notification.is_read)
            self.assertIsInstance(notification.created_at, datetime)
            self.assertEqual(notification.tenant, tenant)
            self.assertEqual(notification.recipient, recipient)
            self.assertIn(notification, tenant.notifications)
            self.assertIn(notification, recipient.received_notifications)

        index_names = {
            index["name"]
            for index in inspect(self.engine).get_indexes("notifications")
        }
        self.assertSetEqual(
            index_names,
            {
                "ix_notifications_tenant_recipient_read_created",
                "ix_notifications_tenant_created_at",
                "ix_notifications_tenant_event_type",
                "ix_notifications_tenant_entity",
            },
        )

    def test_recipient_foreign_key_cannot_cross_tenants(self) -> None:
        with Session(self.engine) as db:
            tenant = Tenant(name="Tenant One")
            other_tenant = Tenant(name="Tenant Two")
            recipient = User(
                tenant=tenant,
                name="Recipient",
                email="recipient@example.test",
                password_hash="hash",
            )
            db.add_all([tenant, other_tenant, recipient])
            db.flush()
            notification = Notification(
                tenant=other_tenant,
                recipient_id=recipient.id,
                event_type="task_assigned",
                title="Task assigned",
                message="This must not cross tenants.",
            )
            db.add(notification)
            with self.assertRaises(IntegrityError):
                db.commit()

    def test_migration_revision_is_current_head(self) -> None:
        migration_path = (
            Path(__file__).parents[2]
            / "alembic"
            / "versions"
            / "7a4d9e2c1f63_create_notifications_table.py"
        )
        spec = importlib.util.spec_from_file_location(
            "create_notifications_table", migration_path
        )
        self.assertIsNotNone(spec)
        assert spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        self.assertEqual(migration.revision, "7a4d9e2c1f63")
        self.assertEqual(migration.down_revision, "48ca2bb8ef13")


if __name__ == "__main__":
    unittest.main()
