import logging
import os
from datetime import datetime, timedelta, timezone

from celery.exceptions import Retry
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from backend.app.celery_app import celery_app
from backend.app.database import SessionLocal
from backend.app.models import Document
from backend.app.services.document_jobs import enqueue_document_cleanup
from backend.app.services.s3_storage import StorageServiceError, s3_storage
from backend.app.utils.audit import create_audit_log

logger = logging.getLogger(__name__)
MAX_RETRIES = 5
MAX_RETRY_DELAY_SECONDS = 300
PENDING_UPLOAD_TTL_SECONDS = int(
    os.getenv("DOCUMENT_PENDING_UPLOAD_TTL_SECONDS", "86400")
)
CLEANUP_RECOVERY_GRACE_SECONDS = int(
    os.getenv("DOCUMENT_CLEANUP_RECOVERY_GRACE_SECONDS", "900")
)
if PENDING_UPLOAD_TTL_SECONDS < 1:
    raise ValueError("DOCUMENT_PENDING_UPLOAD_TTL_SECONDS must be at least 1")
if CLEANUP_RECOVERY_GRACE_SECONDS < 1:
    raise ValueError(
        "DOCUMENT_CLEANUP_RECOVERY_GRACE_SECONDS must be at least 1"
    )


def _retry_delay(retry_count: int) -> int:
    return min(2 ** (retry_count + 1), MAX_RETRY_DELAY_SECONDS)


def _task_context(
    tenant_id: int, document_id: int, task_id: str | None
) -> dict[str, int | str]:
    return {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "celery_task_id": task_id or "unknown",
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@celery_app.task(
    bind=True,
    name="backend.app.tasks.documents.cleanup_object",
    max_retries=MAX_RETRIES,
    soft_time_limit=30,
    time_limit=45,
    acks_late=True,
    reject_on_worker_lost=True,
)
def cleanup_document_object(
    self,
    tenant_id: int,
    document_id: int,
    recover_pending_upload: bool = False,
) -> dict[str, int | str]:
    if (
        type(tenant_id) is not int
        or tenant_id < 1
        or type(document_id) is not int
        or document_id < 1
        or not isinstance(recover_pending_upload, bool)
    ):
        raise ValueError("Invalid document cleanup job arguments")

    context = _task_context(tenant_id, document_id, self.request.id)
    now = datetime.now(timezone.utc)
    pending_cutoff = now - timedelta(seconds=PENDING_UPLOAD_TTL_SECONDS)
    try:
        with SessionLocal() as db:
            document = db.execute(
                select(Document).where(
                    Document.id == document_id,
                    Document.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if document is None:
                logger.info(
                    "document_cleanup_not_found",
                    extra={
                        **context,
                        "event_name": "document_cleanup_not_found",
                    },
                )
                return {"status": "not_found", **context}
            if document.status == "deleted":
                return {"status": "already_deleted", **context}
            if document.status == "pending_upload":
                if (
                    not recover_pending_upload
                    or _as_utc(document.created_at) > pending_cutoff
                ):
                    return {"status": "not_eligible", **context}
                claimed = db.execute(
                    update(Document)
                    .where(
                        Document.id == document_id,
                        Document.tenant_id == tenant_id,
                        Document.status == "pending_upload",
                        Document.created_at <= pending_cutoff,
                    )
                    .values(
                        status="deletion_pending",
                        deleted_at=now,
                        deletion_requested_by=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                db.commit()
                if claimed.rowcount != 1:
                    return {"status": "already_claimed", **context}
                document = db.execute(
                    select(Document).where(
                        Document.id == document_id,
                        Document.tenant_id == tenant_id,
                    )
                ).scalar_one()
            elif document.status != "deletion_pending":
                return {"status": "not_eligible", **context}

            storage_key = document.storage_key
            requested_by = document.deletion_requested_by
            uploaded_by = document.uploaded_by

        try:
            s3_storage.delete_object(storage_key)
        except StorageServiceError as exc:
            if exc.retryable and self.request.retries < MAX_RETRIES:
                delay = _retry_delay(self.request.retries)
                logger.warning(
                    "document_cleanup_retry",
                    extra={
                        **context,
                        "event_name": "document_cleanup_retry",
                        "retry_count": self.request.retries,
                        "retry_delay_seconds": delay,
                        "error_type": type(exc).__name__,
                    },
                )
                raise self.retry(exc=exc, countdown=delay)
            logger.error(
                "document_cleanup_failed",
                extra={
                    **context,
                    "event_name": "document_cleanup_failed",
                    "retry_count": self.request.retries,
                    "retryable": exc.retryable,
                    "error_type": type(exc).__name__,
                },
            )
            raise

        with SessionLocal() as db:
            result = db.execute(
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.tenant_id == tenant_id,
                    Document.status == "deletion_pending",
                )
                .values(status="deleted")
            )
            if result.rowcount == 1:
                actor_id = requested_by or uploaded_by
                create_audit_log(
                    db=db,
                    tenant_id=tenant_id,
                    user_id=actor_id,
                    action=(
                        "document_deleted"
                        if requested_by is not None
                        else "document_upload_abandoned"
                    ),
                    resource_type="document",
                    resource_id=document_id,
                    metadata={
                        "document_id": document_id,
                        "document_status": "deleted",
                    },
                )
            db.commit()

        if result.rowcount == 0:
            logger.info(
                "document_cleanup_duplicate",
                extra={
                    **context,
                    "event_name": "document_cleanup_duplicate",
                },
            )
            return {"status": "already_deleted", **context}

        logger.info(
            "document_cleanup_succeeded",
            extra={
                **context,
                "event_name": "document_cleanup_succeeded",
            },
        )
        return {"status": "deleted", **context}
    except Retry:
        raise
    except StorageServiceError:
        raise
    except OperationalError as exc:
        if self.request.retries < MAX_RETRIES:
            delay = _retry_delay(self.request.retries)
            logger.warning(
                "document_cleanup_database_retry",
                extra={
                    **context,
                    "event_name": "document_cleanup_database_retry",
                    "retry_count": self.request.retries,
                    "retry_delay_seconds": delay,
                    "error_type": type(exc).__name__,
                },
            )
            raise self.retry(exc=exc, countdown=delay)
        logger.error(
            "document_cleanup_database_failed",
            extra={
                **context,
                "event_name": "document_cleanup_database_failed",
                "retry_count": self.request.retries,
                "error_type": type(exc).__name__,
            },
        )
        raise
    except Exception as exc:
        logger.error(
            "document_cleanup_job_failed",
            extra={
                **context,
                "event_name": "document_cleanup_job_failed",
                "retry_count": self.request.retries,
                "error_type": type(exc).__name__,
            },
        )
        raise


@celery_app.task(
    bind=True,
    name="backend.app.tasks.documents.recover_pending_uploads",
    max_retries=MAX_RETRIES,
    soft_time_limit=270,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
)
def recover_document_cleanups(self) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    pending_cutoff = now - timedelta(seconds=PENDING_UPLOAD_TTL_SECONDS)
    deletion_cutoff = now - timedelta(seconds=CLEANUP_RECOVERY_GRACE_SECONDS)
    queued = 0
    try:
        with SessionLocal() as db:
            candidates = db.execute(
                select(Document.tenant_id, Document.id, Document.status).where(
                    (
                        (Document.status == "pending_upload")
                        & (Document.created_at <= pending_cutoff)
                    )
                    | (
                        (Document.status == "deletion_pending")
                        & (Document.updated_at <= deletion_cutoff)
                    )
                ).order_by(Document.tenant_id, Document.id)
            ).yield_per(500)
            for tenant_id, document_id, current_status in candidates:
                queued += int(
                    enqueue_document_cleanup(
                        tenant_id,
                        document_id,
                        recover_pending_upload=current_status == "pending_upload",
                    )
                )
        logger.info(
            "document_cleanup_recovery_scan_succeeded",
            extra={
                "event_name": "document_cleanup_recovery_scan_succeeded",
                "queued_count": queued,
                "retry_count": self.request.retries,
            },
        )
        return {"queued": queued}
    except Retry:
        raise
    except OperationalError as exc:
        if self.request.retries < MAX_RETRIES:
            delay = _retry_delay(self.request.retries)
            logger.warning(
                "document_cleanup_recovery_database_retry",
                extra={
                    "event_name": "document_cleanup_recovery_database_retry",
                    "retry_count": self.request.retries,
                    "retry_delay_seconds": delay,
                    "error_type": type(exc).__name__,
                },
            )
            raise self.retry(exc=exc, countdown=delay)
        logger.error(
            "document_cleanup_recovery_failed",
            extra={
                "event_name": "document_cleanup_recovery_failed",
                "retry_count": self.request.retries,
                "error_type": type(exc).__name__,
            },
        )
        raise
    except Exception as exc:
        logger.error(
            "document_cleanup_recovery_failed",
            extra={
                "event_name": "document_cleanup_recovery_failed",
                "retry_count": self.request.retries,
                "error_type": type(exc).__name__,
            },
        )
        raise
