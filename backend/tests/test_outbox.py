import unittest
from datetime import datetime

from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.events import EventPublisher, TaskAssigned
from backend.app.models import Base, OutboxEvent, Tenant
from backend.app.outbox import (
    MAX_RETRIES,
    claim_next_event,
    process_event,
    process_next_event,
    store_event,
    UnhandledEventError,
)


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            db.add(Tenant(name="Tenant One"))
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    def test_event_is_atomic_with_business_transaction(self) -> None:
        with Session(self.engine) as db:
            db.add(Tenant(name="Business Tenant"))
            tenant = db.scalars(
                select(Tenant).where(Tenant.name == "Business Tenant")
            ).one()
            tenant_id = tenant.id
            store_event(
                db,
                TaskAssigned(
                    tenant_id=tenant_id,
                    task_id=11,
                    assigned_to=23,
                    assigned_by=19,
                ),
            )
            db.rollback()

        with Session(self.engine) as db:
            self.assertIsNone(
                db.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.tenant_id == tenant_id
                    )
                ).first()
            )

    def test_processing_is_idempotent_for_duplicate_delivery(self) -> None:
        with Session(self.engine) as db:
            event_row = store_event(
                db,
                TaskAssigned(1, 11, 23, 19),
            )
            db.commit()
            claimed = claim_next_event(db)
            self.assertEqual(claimed.id, event_row.id)

            received: list[TaskAssigned] = []
            publisher = EventPublisher()
            publisher.register(
                TaskAssigned, received.append, tenant_id=1
            )
            self.assertTrue(process_event(db, claimed, publisher))
            self.assertFalse(process_event(db, claimed, publisher))
            self.assertEqual(received, [TaskAssigned(1, 11, 23, 19)])

    def test_retries_are_bounded_and_recorded(self) -> None:
        with Session(self.engine) as db:
            store_event(db, TaskAssigned(1, 11, 23, 19))
            db.commit()
            publisher = EventPublisher()
            publisher.register(
                TaskAssigned,
                lambda event: (_ for _ in ()).throw(
                    RuntimeError("handler unavailable")
                ),
                tenant_id=1,
            )

            for attempt in range(MAX_RETRIES):
                claimed = claim_next_event(db)
                self.assertIsNotNone(claimed)
                with self.assertRaises(RuntimeError):
                    process_event(db, claimed, publisher)
                db.refresh(claimed)
                self.assertEqual(claimed.retry_count, attempt + 1)

            self.assertEqual(claimed.status, "failed")
            self.assertIn("handler unavailable", claimed.error_details)
            self.assertIsNotNone(claimed.created_at)

    def test_claiming_removes_event_from_next_worker(self) -> None:
        with Session(self.engine) as db:
            store_event(db, TaskAssigned(1, 11, 23, 19))
            db.commit()
            first_claim = claim_next_event(db)
            second_claim = claim_next_event(db)

            self.assertIsNotNone(first_claim)
            self.assertIsNone(second_claim)
            self.assertEqual(first_claim.status, "processing")

    def test_process_next_event_and_indexes(self) -> None:
        with Session(self.engine) as db:
            store_event(db, TaskAssigned(1, 11, 23, 19))
            db.commit()
            received: list[TaskAssigned] = []
            publisher = EventPublisher()
            publisher.register(
                TaskAssigned, received.append, tenant_id=1
            )

            self.assertTrue(process_next_event(db, publisher, tenant_id=1))
            self.assertFalse(process_next_event(db, publisher, tenant_id=1))
            row = db.scalars(select(OutboxEvent)).one()
            self.assertEqual(row.status, "processed")
            self.assertIsInstance(row.processed_at, datetime)

        index_names = {
            index["name"]
            for index in inspect(self.engine).get_indexes("outbox_events")
        }
        self.assertSetEqual(
            index_names,
            {
                "ix_outbox_events_tenant_status_created",
                "ix_outbox_events_status_created",
                "ix_outbox_events_tenant_event_type",
            },
        )

    def test_missing_handler_is_retryable_and_not_processed(self) -> None:
        with Session(self.engine) as db:
            row = store_event(db, TaskAssigned(1, 11, 23, 19))
            db.commit()
            claimed = claim_next_event(db)
            with self.assertRaises(UnhandledEventError):
                process_event(db, claimed, EventPublisher())
            db.refresh(row)
            self.assertEqual(row.status, "pending")
            self.assertEqual(row.retry_count, 1)
            self.assertIsNone(row.processed_at)
            self.assertIn("No handler registered", row.error_details)

    def test_handler_for_another_tenant_does_not_process_event(self) -> None:
        with Session(self.engine) as db:
            row = store_event(db, TaskAssigned(1, 11, 23, 19))
            db.commit()
            claimed = claim_next_event(db)
            publisher = EventPublisher()
            publisher.register(
                TaskAssigned,
                lambda event: None,
                tenant_id=2,
            )

            with self.assertRaises(UnhandledEventError):
                process_event(db, claimed, publisher)

            db.refresh(row)
            self.assertEqual(row.status, "pending")
            self.assertIsNone(row.processed_at)


if __name__ == "__main__":
    unittest.main()
