import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

with patch.dict(
    os.environ,
    {
        "DATABASE_URL": "sqlite://",
        "JWT_SECRET_KEY": "notification-api-test-signing-key",
    },
):
    from backend.app.database import get_db
    from backend.app.events import (
        DocumentDeleted,
        DocumentUploaded,
        EventPublisher,
        ProjectStatusChanged,
        TaskAssigned,
        TaskStatusChanged,
    )
    from backend.app.event_handlers import register_production_handlers
    from backend.app.main import app
    from backend.app.models import Base, Notification, OutboxEvent, Tenant, User
    from backend.app.outbox import claim_next_event, process_event, store_event
    from backend.app.utils.jwt import create_access_token


class NotificationTests(unittest.TestCase):
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
            tenant = Tenant(name="Notification Tenant")
            other_tenant = Tenant(name="Other Notification Tenant")
            db.add_all([tenant, other_tenant])
            db.flush()
            self.owner = User(
                tenant_id=tenant.id,
                name="Owner",
                email="notification-owner@example.test",
                password_hash="hash",
                role="owner",
            )
            self.recipient = User(
                tenant_id=tenant.id,
                name="Recipient",
                email="notification-recipient@example.test",
                password_hash="hash",
                role="member",
            )
            self.other_owner = User(
                tenant_id=other_tenant.id,
                name="Other Owner",
                email="notification-other@example.test",
                password_hash="hash",
                role="owner",
            )
            db.add_all([self.owner, self.recipient, self.other_owner])
            db.commit()
            self.tenant_id = tenant.id
            self.other_tenant_id = other_tenant.id
            self.recipient_id = self.recipient.id
            self.owner_id = self.owner.id
            self.other_owner_id = self.other_owner.id

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
    def _headers(user_id: int, tenant_id: int) -> dict[str, str]:
        token = create_access_token(
            {"sub": str(user_id), "tenant_id": tenant_id, "role": "member"}
        )
        return {"Authorization": f"Bearer {token}"}

    def test_task_assignment_delivers_once_and_duplicate_processing_is_safe(self):
        publisher = EventPublisher()
        register_production_handlers(publisher)
        with self.session_factory() as db:
            store_event(
                db,
                TaskAssigned(
                    tenant_id=self.tenant_id,
                    task_id=42,
                    assigned_to=self.recipient_id,
                    assigned_by=self.owner_id,
                ),
            )
            db.commit()
            row = claim_next_event(db)
            self.assertTrue(process_event(db, row, publisher))
            self.assertFalse(process_event(db, row, publisher))
            notifications = db.query(Notification).all()
            self.assertEqual(len(notifications), 1)
            self.assertEqual(notifications[0].recipient_id, self.recipient_id)

    def test_existing_source_notification_is_idempotent_and_processes_event(self):
        publisher = EventPublisher()
        register_production_handlers(publisher)
        with self.session_factory() as db:
            event = TaskAssigned(
                tenant_id=self.tenant_id,
                task_id=42,
                assigned_to=self.recipient_id,
                assigned_by=self.owner_id,
            )
            store_event(db, event)
            db.commit()
            row = claim_next_event(db)
            db.add(
                Notification(
                    tenant_id=self.tenant_id,
                    recipient_id=self.recipient_id,
                    source_event_id=row.id,
                    event_type="TaskAssigned",
                    title="Task assigned to you",
                    message="Already delivered",
                    entity_type="task",
                    entity_id=event.task_id,
                )
            )
            db.commit()

            self.assertTrue(process_event(db, row, publisher))
            db.refresh(row)
            self.assertEqual(row.status, "processed")
            self.assertEqual(db.query(Notification).count(), 1)
            self.assertEqual(row.retry_count, 0)

    def test_assignment_to_foreign_user_fails_without_notification(self):
        publisher = EventPublisher()
        register_production_handlers(publisher)
        with self.session_factory() as db:
            store_event(
                db,
                TaskAssigned(
                    tenant_id=self.tenant_id,
                    task_id=42,
                    assigned_to=self.other_owner_id,
                    assigned_by=self.owner_id,
                ),
            )
            db.commit()
            row = claim_next_event(db)
            with self.assertRaises(ValueError):
                process_event(db, row, publisher)
            db.refresh(row)
            self.assertEqual(row.status, "pending")
            self.assertEqual(db.query(Notification).count(), 0)

    def test_all_production_event_types_process_without_guessed_recipients(self):
        publisher = EventPublisher()
        register_production_handlers(publisher)
        events = (
            TaskAssigned(
                self.tenant_id, 42, self.recipient_id, self.owner_id
            ),
            TaskStatusChanged(
                self.tenant_id, 42, "todo", "in_progress", self.owner_id
            ),
            ProjectStatusChanged(
                self.tenant_id, 7, "planned", "active", self.owner_id
            ),
            DocumentUploaded(
                self.tenant_id, 9, 7, self.owner_id
            ),
            DocumentDeleted(
                self.tenant_id, 9, 7, self.owner_id
            ),
        )
        with self.session_factory() as db:
            for event in events:
                store_event(db, event)
            db.commit()
            for _ in events:
                row = claim_next_event(db)
                self.assertIsNotNone(row)
                self.assertTrue(process_event(db, row, publisher))

            rows = db.query(OutboxEvent).all()
            self.assertEqual([row.status for row in rows], ["processed"] * 5)
            self.assertEqual(db.query(Notification).count(), 1)

    def test_assignment_with_foreign_actor_is_rejected(self):
        publisher = EventPublisher()
        register_production_handlers(publisher)
        with self.session_factory() as db:
            store_event(
                db,
                TaskAssigned(
                    self.tenant_id,
                    42,
                    self.recipient_id,
                    self.other_owner_id,
                ),
            )
            db.commit()
            row = claim_next_event(db)
            with self.assertRaises(ValueError):
                process_event(db, row, publisher)
            db.refresh(row)
            self.assertEqual(row.status, "pending")
            self.assertEqual(row.retry_count, 1)
            self.assertEqual(db.query(Notification).count(), 0)

    def test_handler_database_writes_rollback_before_retry_state(self):
        publisher = EventPublisher()

        def failing_handler(event, db, event_id):
            db.add(
                Notification(
                    tenant_id=event.tenant_id,
                    recipient_id=self.recipient_id,
                    source_event_id=event_id,
                    event_type="TaskAssigned",
                    title="Should roll back",
                    message="Temporary failure",
                )
            )
            db.flush()
            raise RuntimeError("handler failed")

        publisher.register(
            TaskAssigned,
            failing_handler,
            tenant_id=self.tenant_id,
            uses_db=True,
        )
        with self.session_factory() as db:
            store_event(
                db,
                TaskAssigned(
                    self.tenant_id,
                    42,
                    self.recipient_id,
                    self.owner_id,
                ),
            )
            db.commit()
            row = claim_next_event(db)
            with self.assertRaises(RuntimeError):
                process_event(db, row, publisher)
            db.refresh(row)
            self.assertEqual(row.status, "pending")
            self.assertEqual(row.retry_count, 1)
            self.assertEqual(db.query(Notification).count(), 0)

    def test_notification_api_owns_tenant_and_supports_read_operations(self):
        with self.session_factory() as db:
            for index in range(3):
                db.add(
                    Notification(
                        tenant_id=self.tenant_id,
                        recipient_id=self.recipient_id,
                        event_type="TaskAssigned",
                        title=f"Task {index}",
                        message="Assigned",
                    )
                )
            db.add(
                Notification(
                    tenant_id=self.other_tenant_id,
                    recipient_id=self.other_owner_id,
                    event_type="TaskAssigned",
                    title="Foreign",
                    message="Private",
                )
            )
            db.commit()

        headers = self._headers(self.recipient_id, self.tenant_id)
        page = self.client.get(
            "/notifications",
            headers=headers,
            params={"page": 2, "page_size": 2},
        )
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["total"], 3)
        self.assertEqual(len(page.json()["items"]), 1)

        unread = self.client.get("/notifications/unread-count", headers=headers)
        self.assertEqual(unread.json(), {"unread_count": 3})
        notification_id = page.json()["items"][0]["id"]
        read = self.client.patch(
            f"/notifications/{notification_id}/read", headers=headers
        )
        self.assertEqual(read.status_code, 200, read.text)
        self.assertTrue(read.json()["is_read"])

        read_all = self.client.patch("/notifications/read-all", headers=headers)
        self.assertEqual(read_all.json(), {"updated_count": 2})
        self.assertEqual(
            self.client.get(
                "/notifications/unread-count", headers=headers
            ).json(),
            {"unread_count": 0},
        )

    def test_other_tenant_notification_is_not_accessible(self):
        with self.session_factory() as db:
            foreign = Notification(
                tenant_id=self.other_tenant_id,
                recipient_id=self.other_owner_id,
                event_type="TaskAssigned",
                title="Foreign",
                message="Private",
            )
            db.add(foreign)
            db.commit()
            foreign_id = foreign.id

        response = self.client.patch(
            f"/notifications/{foreign_id}/read",
            headers=self._headers(self.recipient_id, self.tenant_id),
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
