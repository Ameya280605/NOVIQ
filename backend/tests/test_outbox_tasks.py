import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from celery.schedules import crontab
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.celery_app import celery_app
from backend.app.events import TaskAssigned, event_publisher
from backend.app.models import Base, OutboxEvent, Tenant
from backend.app.outbox import store_event
from backend.app.tasks import outbox as outbox_tasks


class OutboxTaskTests(unittest.TestCase):
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
            db.add(Tenant(name="Outbox Task Tenant"))
            db.commit()
        self.original = list(event_publisher._handlers)
        event_publisher.clear()
        self.session_patch = patch.object(
            outbox_tasks, "SessionLocal", self.session_factory
        )
        self.session_patch.start()
        self.eager_settings = (
            celery_app.conf.task_always_eager,
            celery_app.conf.task_eager_propagates,
        )
        celery_app.conf.update(
            task_always_eager=True,
            task_eager_propagates=True,
        )

    def tearDown(self) -> None:
        event_publisher.clear()
        event_publisher._handlers.extend(self.original)
        self.session_patch.stop()
        celery_app.conf.update(
            task_always_eager=self.eager_settings[0],
            task_eager_propagates=self.eager_settings[1],
        )
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    def test_task_processes_pending_event_and_is_idempotent(self) -> None:
        received: list[TaskAssigned] = []
        event_publisher.register(TaskAssigned, received.append, tenant_id=1)
        with self.session_factory() as db:
            store_event(db, TaskAssigned(1, 11, 23, 19))
            db.commit()

        first = outbox_tasks.process_pending_events.apply(throw=True).result
        second = outbox_tasks.process_pending_events.apply(throw=True).result

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(received, [TaskAssigned(1, 11, 23, 19)])

    def test_task_records_handler_retry_without_losing_batch(self) -> None:
        event_publisher.register(
            TaskAssigned,
            lambda event: (_ for _ in ()).throw(RuntimeError("temporary")),
            tenant_id=1,
        )
        with self.session_factory() as db:
            store_event(db, TaskAssigned(1, 11, 23, 19))
            db.commit()

        result = outbox_tasks.process_pending_events.apply(throw=True).result
        with self.session_factory() as db:
            row = db.scalars(select(OutboxEvent)).one()
            self.assertEqual(row.status, "pending")
            self.assertEqual(row.retry_count, 1)
        self.assertEqual(result["failed"], 1)

    def test_task_recovers_stale_processing_event(self) -> None:
        with self.session_factory() as db:
            row = store_event(db, TaskAssigned(1, 11, 23, 19))
            db.commit()
            row.status = "processing"
            row.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            db.commit()

        event_publisher.register(TaskAssigned, lambda event: None, tenant_id=1)
        result = outbox_tasks.process_pending_events.apply(throw=True).result

        with self.session_factory() as db:
            row = db.scalars(select(OutboxEvent)).one()
            self.assertEqual(row.status, "processed")
            self.assertEqual(row.retry_count, 1)
        self.assertEqual(result["recovered"], 1)

    def test_task_fails_stale_event_at_retry_limit_instead_of_leaving_it_processing(
        self,
    ) -> None:
        with self.session_factory() as db:
            row = store_event(db, TaskAssigned(1, 11, 23, 19))
            db.commit()
            row.status = "processing"
            row.retry_count = 3
            row.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            db.commit()

        result = outbox_tasks.process_pending_events.apply(throw=True).result

        with self.session_factory() as db:
            row = db.scalars(select(OutboxEvent)).one()
            self.assertEqual(row.status, "failed")
            self.assertEqual(row.retry_count, 3)
            self.assertIsNone(row.claimed_at)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(result["processed"], 0)

    def test_beat_schedules_outbox_polling(self) -> None:
        entry = celery_app.conf.beat_schedule[
            "schedule-outbox-event-processing-every-minute"
        ]
        self.assertEqual(
            entry["task"],
            "backend.app.tasks.outbox.process_pending_events",
        )
        self.assertEqual(entry["schedule"], crontab(minute="*"))


if __name__ == "__main__":
    unittest.main()
