import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Type

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from backend.app.events import (
    DocumentDeleted,
    DocumentUploaded,
    DomainEvent,
    EventPublisher,
    ProjectStatusChanged,
    TaskAssigned,
    TaskStatusChanged,
)
from backend.app.models import OutboxEvent

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
STALE_PROCESSING_SECONDS = 300


class UnhandledEventError(RuntimeError):
    pass

_EVENT_TYPES: dict[str, Type[DomainEvent]] = {
    event_type.__name__: event_type
    for event_type in (
        TaskAssigned,
        TaskStatusChanged,
        ProjectStatusChanged,
        DocumentUploaded,
        DocumentDeleted,
    )
}


def store_event(db: Session, event: DomainEvent) -> OutboxEvent:
    event_type = type(event)
    if event_type.__name__ not in _EVENT_TYPES:
        raise ValueError(f"Unsupported domain event type: {event_type.__name__}")

    outbox_event = OutboxEvent(
        tenant_id=event.tenant_id,
        event_type=event_type.__name__,
        event_payload=asdict(event),
    )
    db.add(outbox_event)
    db.flush()
    return outbox_event


def claim_next_event(
    db: Session,
    *,
    tenant_id: int | None = None,
) -> OutboxEvent | None:
    query = (
        select(OutboxEvent)
        .where(
            OutboxEvent.status == "pending",
            OutboxEvent.retry_count < MAX_RETRIES,
        )
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .with_for_update(skip_locked=True)
    )
    if tenant_id is not None:
        query = query.where(OutboxEvent.tenant_id == tenant_id)

    outbox_event = db.execute(query).scalars().first()
    if outbox_event is None:
        return None

    outbox_event.status = "processing"
    outbox_event.claimed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(outbox_event)
    return outbox_event


def process_event(
    db: Session,
    outbox_event: OutboxEvent,
    publisher: EventPublisher,
) -> bool:
    if outbox_event.status == "processed":
        return False
    if outbox_event.status != "processing":
        raise ValueError("Outbox event must be claimed before processing")

    try:
        event = _deserialize_event(outbox_event)
        if publisher.publish(
            event,
            db=db,
            event_id=outbox_event.id,
        ) == 0:
            raise UnhandledEventError(
                f"No handler registered for {outbox_event.event_type}"
            )
    except Exception as exc:
        db.rollback()
        outbox_event = db.get(OutboxEvent, outbox_event.id)
        if outbox_event is None:
            raise RuntimeError("Outbox event disappeared during processing") from exc
        outbox_event.retry_count += 1
        outbox_event.status = (
            "failed" if outbox_event.retry_count >= MAX_RETRIES else "pending"
        )
        outbox_event.claimed_at = None
        outbox_event.error_details = f"{type(exc).__name__}: {exc}"[:2000]
        db.commit()
        logger.error(
            "outbox_event_failed",
            extra={
                "event_name": "outbox_event_failed",
                "event_id": outbox_event.id,
                "tenant_id": outbox_event.tenant_id,
                "event_type": outbox_event.event_type,
                "retry_count": outbox_event.retry_count,
                "status": outbox_event.status,
                "error_type": type(exc).__name__,
            },
        )
        raise

    outbox_event.status = "processed"
    outbox_event.claimed_at = None
    outbox_event.processed_at = datetime.now(timezone.utc)
    outbox_event.error_details = None
    db.commit()
    return True


def recover_stale_events(
    db: Session,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_PROCESSING_SECONDS,
) -> int:
    if stale_after_seconds < 1:
        raise ValueError("stale_after_seconds must be positive")
    cutoff = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    )
    cutoff = cutoff.replace(
        microsecond=cutoff.microsecond
    )
    cutoff = cutoff - timedelta(seconds=stale_after_seconds)
    result = db.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.status == "processing",
            OutboxEvent.claimed_at.is_not(None),
            OutboxEvent.claimed_at < cutoff,
        )
        .values(
            status=case(
                (OutboxEvent.retry_count >= MAX_RETRIES, "failed"),
                (OutboxEvent.retry_count + 1 >= MAX_RETRIES, "failed"),
                else_="pending",
            ),
            claimed_at=None,
            retry_count=case(
                (OutboxEvent.retry_count >= MAX_RETRIES, OutboxEvent.retry_count),
                else_=OutboxEvent.retry_count + 1,
            ),
            error_details="Recovered stale processing claim",
        )
    )
    db.commit()
    return result.rowcount


def process_next_event(
    db: Session,
    publisher: EventPublisher,
    *,
    tenant_id: int | None = None,
) -> bool:
    outbox_event = claim_next_event(db, tenant_id=tenant_id)
    if outbox_event is None:
        return False
    process_event(db, outbox_event, publisher)
    return True


def _deserialize_event(outbox_event: OutboxEvent) -> DomainEvent:
    event_type = _EVENT_TYPES.get(outbox_event.event_type)
    if event_type is None:
        raise ValueError(f"Unsupported domain event type: {outbox_event.event_type}")

    payload = dict(outbox_event.event_payload)
    if payload.get("tenant_id") != outbox_event.tenant_id:
        raise ValueError("Outbox event payload tenant does not match row tenant")
    return event_type(**payload)
