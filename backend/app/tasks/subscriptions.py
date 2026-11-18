import logging

from celery.exceptions import Retry
from sqlalchemy.exc import OperationalError

from backend.app.celery_app import celery_app
from backend.app.database import SessionLocal
from backend.app.services.subscriptions import (
    expire_subscriptions as expire_subscription_records,
)

logger = logging.getLogger(__name__)
MAX_RETRIES = 3


@celery_app.task(
    bind=True,
    name="backend.app.tasks.subscriptions.expire_subscriptions",
    max_retries=MAX_RETRIES,
    soft_time_limit=30,
    time_limit=45,
    acks_late=True,
    reject_on_worker_lost=True,
)
def expire_subscriptions(self) -> dict[str, int]:
    try:
        with SessionLocal() as db:
            expired_count = expire_subscription_records(db)
            return {"expired_count": expired_count}
    except Retry:
        raise
    except OperationalError as exc:
        if self.request.retries < MAX_RETRIES:
            raise self.retry(
                exc=exc,
                countdown=min(2 ** self.request.retries, 30),
            )
        logger.error(
            "subscription_expiry_failed",
            extra={
                "event_name": "subscription_expiry_failed",
                "error_type": type(exc).__name__,
            },
        )
        raise
