import logging

from kombu.exceptions import OperationalError
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def enqueue_document_cleanup(
    tenant_id: int,
    document_id: int,
    *,
    recover_pending_upload: bool = False,
) -> bool:
    try:
        from backend.app.tasks.documents import cleanup_document_object

        result = cleanup_document_object.apply_async(
            args=(tenant_id, document_id, recover_pending_upload)
        )
    except (OperationalError, RedisError, OSError, TimeoutError) as exc:
        logger.warning(
            "document_cleanup_enqueue_failed",
            extra={
                "event_name": "document_cleanup_enqueue_failed",
                "tenant_id": tenant_id,
                "document_id": document_id,
                "error_type": type(exc).__name__,
            },
        )
        return False

    logger.info(
        "document_cleanup_enqueued",
        extra={
            "event_name": "document_cleanup_enqueued",
            "tenant_id": tenant_id,
            "document_id": document_id,
            "celery_task_id": result.id,
        },
    )
    return True
