import unittest

from backend.app.events import (
    DocumentDeleted,
    DocumentUploaded,
    EventPublisher,
    ProjectStatusChanged,
    TaskAssigned,
    TaskStatusChanged,
)
from backend.app.event_handlers import register_production_handlers


class DomainEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.publisher = EventPublisher()

    def test_publishes_typed_events_to_matching_tenant_handlers(self) -> None:
        received: list[TaskAssigned] = []
        unregister = self.publisher.register(
            TaskAssigned,
            received.append,
            tenant_id=7,
        )
        event = TaskAssigned(
            tenant_id=7,
            task_id=11,
            assigned_to=23,
            assigned_by=19,
        )

        self.assertEqual(self.publisher.publish(event), 1)
        self.assertEqual(received, [event])

        unregister()
        self.assertEqual(self.publisher.publish(event), 0)
        self.assertEqual(received, [event])

    def test_does_not_dispatch_events_across_tenants_or_event_types(self) -> None:
        tenant_one_events: list[TaskStatusChanged] = []
        tenant_two_events: list[TaskStatusChanged] = []
        assigned_events: list[TaskAssigned] = []
        self.publisher.register(
            TaskStatusChanged,
            tenant_one_events.append,
            tenant_id=1,
        )
        self.publisher.register(
            TaskStatusChanged,
            tenant_two_events.append,
            tenant_id=2,
        )
        self.publisher.register(
            TaskAssigned,
            assigned_events.append,
            tenant_id=1,
        )

        event = TaskStatusChanged(
            tenant_id=2,
            task_id=11,
            previous_status="todo",
            new_status="in_progress",
            changed_by=23,
        )

        self.assertEqual(self.publisher.publish(event), 1)
        self.assertEqual(tenant_one_events, [])
        self.assertEqual(tenant_two_events, [event])
        self.assertEqual(assigned_events, [])

    def test_supports_all_phase_two_event_payloads(self) -> None:
        events = [
            TaskAssigned(7, 11, 23, 19),
            TaskStatusChanged(7, 11, "todo", "completed", 19),
            ProjectStatusChanged(7, 31, "planned", "active", 19),
            DocumentUploaded(7, 41, 31, 23),
            DocumentDeleted(7, 41, 31, 19),
        ]

        received: list[object] = []
        for event in events:
            self.publisher.register(type(event), received.append, tenant_id=7)

        self.assertEqual(
            sum(self.publisher.publish(event) for event in events),
            len(events),
        )
        self.assertEqual(received, events)

    def test_rejects_invalid_tenant_registration_and_event(self) -> None:
        with self.assertRaises(ValueError):
            self.publisher.register(
                TaskAssigned,
                lambda event: None,
                tenant_id=0,
            )
        with self.assertRaises(ValueError):
            TaskAssigned(tenant_id=0, task_id=1, assigned_to=2, assigned_by=3)

    def test_production_handlers_register_for_every_tenant(self) -> None:
        register_production_handlers(self.publisher)
        register_production_handlers(self.publisher)

        self.assertEqual(len(self.publisher._handlers), 5)
        self.assertEqual(
            {event_type.__name__ for event_type, _, _, _ in self.publisher._handlers},
            {
                "TaskAssigned",
                "TaskStatusChanged",
                "ProjectStatusChanged",
                "DocumentUploaded",
                "DocumentDeleted",
            },
        )
        self.assertEqual(
            self.publisher.publish(
                TaskStatusChanged(
                    tenant_id=99,
                    task_id=1,
                    previous_status="todo",
                    new_status="completed",
                    changed_by=3,
                )
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
