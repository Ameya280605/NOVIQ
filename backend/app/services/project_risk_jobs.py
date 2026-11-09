import logging
from uuid import uuid4

from kombu.exceptions import OperationalError
from redis.exceptions import RedisError

from backend.app.services import cache
from backend.app.services.project_risk_cache import (
    PROJECT_RISK_VERSION_TTL_SECONDS,
    invalidate_project_risk,
    project_risk_version_key,
)

logger = logging.getLogger(__name__)


def enqueue_project_risk_recalculation(
    tenant_id: int,
    project_id: int,
) -> str | None:
    version = str(uuid4())
    version_key = project_risk_version_key(tenant_id, project_id)
    version_registered = True
    try:
        cache.set_value(version_key, version, PROJECT_RISK_VERSION_TTL_SECONDS)
    except (RedisError, OSError, TimeoutError) as exc:
        logger.warning(
            "project_risk_job_version_write_failed",
            extra={
                "event_name": "project_risk_job_version_write_failed",
                "tenant_id": tenant_id,
                "project_id": project_id,
                "error_type": type(exc).__name__,
            },
        )
        version_registered = False

    try:
        from backend.app.tasks.project_risk import recalculate_project_risk

        result = recalculate_project_risk.apply_async(
            args=(tenant_id, project_id, version, version_registered)
        )
    except (OperationalError, RedisError, OSError, TimeoutError) as exc:
        logger.warning(
            "project_risk_job_enqueue_failed",
            extra={
                "event_name": "project_risk_job_enqueue_failed",
                "tenant_id": tenant_id,
                "project_id": project_id,
                "recovery": "periodic_scan",
                "error_type": type(exc).__name__,
            },
        )
        return None
    logger.info(
        "project_risk_job_enqueued",
        extra={
            "event_name": "project_risk_job_enqueued",
            "tenant_id": tenant_id,
            "project_id": project_id,
            "celery_task_id": result.id,
        },
    )
    return result.id


def refresh_project_risk_after_mutation(
    tenant_id: int,
    project_ids: set[int] | list[int] | tuple[int, ...],
) -> None:
    for project_id in sorted(set(project_ids)):
        invalidate_project_risk(tenant_id, {project_id})
        enqueue_project_risk_recalculation(tenant_id, project_id)
