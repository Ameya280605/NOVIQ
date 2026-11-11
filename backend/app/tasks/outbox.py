import logging

from celery.exceptions import Retry
from sqlalchemy.exc import OperationalError

from backend.app.celery_app import celery_app
from backend.app.database import SessionLocal
from backend.app.events import event_publisher
from backend.app.event_handlers import register_production_handlers
from backend.app.outbox import (
    MAX_RETRIES,
    STALE_PROCESSING_SECONDS,
    process_next_event,
    recover_stale_events,
)

logger = logging.getLogger(__name__)
POLL_BATCH_SIZE = 50

register_production_handlers(event_publisher)


@celery_app.task(
    bind=True,
    name="backend.app.tasks.outbox.process_pending_events",
    max_retries=MAX_RETRIES,
    soft_time_limit=30,
    time_limit=45,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_pending_events(self) -> dict[str, int]:
    try:
        with SessionLocal() as db:
            recovered = recover_stale_events(
                db,
                stale_after_seconds=STALE_PROCESSING_SECONDS,
            )
            processed = 0
            failed = 0
            for _ in range(POLL_BATCH_SIZE):
                try:
                    if not process_next_event(db, event_publisher):
                        break
                    processed += 1
                except Exception as exc:
                    failed += 1
                    logger.error(
                        "outbox_event_processing_failed",
                        extra={
                            "event_name": "outbox_event_processing_failed",
                            "error_type": type(exc).__name__,
                        },
                    )
                    break
            return {
                "recovered": recovered,
                "processed": processed,
                "failed": failed,
            }
    except Retry:
        raise
    except OperationalError as exc:
        if self.request.retries < MAX_RETRIES:
            raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 30))
        logger.error(
            "outbox_poll_failed",
            extra={
                "event_name": "outbox_poll_failed",
                "error_type": type(exc).__name__,
            },
        )
        raise
