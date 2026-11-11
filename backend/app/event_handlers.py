import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.events import (
    DocumentDeleted,
    DocumentUploaded,
    EventPublisher,
    ProjectStatusChanged,
    TaskAssigned,
    TaskStatusChanged,
)
from backend.app.models import Notification, User

logger = logging.getLogger(__name__)


def _record_event(event: object) -> None:
    logger.info(
        "domain_event_handled",
        extra={
            "event_name": "domain_event_handled",
            "event_type": type(event).__name__,
            "tenant_id": getattr(event, "tenant_id"),
        },
    )


def _create_task_assigned_notification(
    event: TaskAssigned,
    db: Session,
    event_id: int,
) -> None:
    actor = db.scalar(
        select(User).where(
            User.id == event.assigned_by,
            User.tenant_id == event.tenant_id,
        )
    )
    if actor is None:
        raise ValueError("Task assignment actor is not in the event tenant")

    recipient = db.scalar(
        select(User).where(
            User.id == event.assigned_to,
            User.tenant_id == event.tenant_id,
        )
    )
    if recipient is None:
        raise ValueError("Task assignment recipient is not in the event tenant")

    existing = db.scalar(
        select(Notification.id).where(
            Notification.source_event_id == event_id,
            Notification.recipient_id == recipient.id,
        )
    )
    if existing is not None:
        return

    try:
        with db.begin_nested():
            db.add(
                Notification(
                    tenant_id=event.tenant_id,
                    recipient_id=recipient.id,
                    source_event_id=event_id,
                    event_type=type(event).__name__,
                    title="Task assigned to you",
                    message=f"Task {event.task_id} was assigned to you.",
                    entity_type="task",
                    entity_id=event.task_id,
                )
            )
            db.flush()
    except IntegrityError:
        duplicate = db.scalar(
            select(Notification.id).where(
                Notification.source_event_id == event_id,
                Notification.recipient_id == recipient.id,
            )
        )
        if duplicate is None:
            raise


def register_production_handlers(publisher: EventPublisher) -> None:
    handlers = (
        (TaskAssigned, _create_task_assigned_notification, True),
        (TaskStatusChanged, _record_event),
        (ProjectStatusChanged, _record_event),
        (DocumentUploaded, _record_event),
        (DocumentDeleted, _record_event),
    )
    for registration in handlers:
        event_type, handler = registration[:2]
        uses_db = registration[2] if len(registration) == 3 else False
        publisher.register(
            event_type,
            handler,
            tenant_id=None,
            uses_db=uses_db,
        )
