import logging
from uuid import uuid4

from celery.exceptions import Retry
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from backend.app.celery_app import celery_app
from backend.app.database import SessionLocal
from backend.app.models import Project
from backend.app.schemas.project_risk import ProjectRiskResponse
from backend.app.services import cache
from backend.app.services.project_risk import SCORING_VERSION, analyze_project_risk
from backend.app.services.project_risk_cache import (
    PROJECT_RISK_CACHE_TTL_SECONDS,
    PROJECT_RISK_VERSION_TTL_SECONDS,
    mark_project_risk_job_complete_if_cached,
    project_risk_cache_key,
    project_risk_job_completion_key,
    project_risk_version_key,
)
from backend.app.services.project_risk_jobs import enqueue_project_risk_recalculation

logger = logging.getLogger(__name__)
MAX_RETRIES = 3
PROJECT_RISK_LOCK_TTL_SECONDS = 60


def _job_context(tenant_id: int, project_id: int, task_id: str) -> dict[str, int | str]:
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "celery_task_id": task_id,
    }


def _retry_delay(retries: int) -> int:
    return min(2 ** retries, 30)


def _cached_risk_matches_project(
    cached_result: object,
    project_id: int,
) -> bool:
    if not isinstance(cached_result, dict):
        return False
    try:
        response = ProjectRiskResponse.model_validate(cached_result)
    except ValidationError:
        return False
    return (
        response.project_id == project_id
        and response.scoring_version == SCORING_VERSION
    )


@celery_app.task(
    bind=True,
    name="backend.app.tasks.project_risk.recalculate",
    max_retries=MAX_RETRIES,
    soft_time_limit=30,
    time_limit=45,
    acks_late=True,
    reject_on_worker_lost=True,
)
def recalculate_project_risk(
    self,
    tenant_id: int,
    project_id: int,
    expected_version: str,
    version_guard_enabled: bool = True,
) -> dict[str, int | str]:
    if (
        type(tenant_id) is not int
        or tenant_id < 1
        or type(project_id) is not int
        or project_id < 1
        or not isinstance(expected_version, str)
        or len(expected_version) > 64
        or not isinstance(version_guard_enabled, bool)
    ):
        raise ValueError("Invalid project risk job arguments")

    context = _job_context(tenant_id, project_id, self.request.id or "unknown")
    cache_key = project_risk_cache_key(tenant_id, project_id)
    version_key = project_risk_version_key(tenant_id, project_id)
    lock_key = f"{cache_key}:job-lock:{expected_version}"
    lock_token = str(uuid4())
    lock_acquired = False
    version_read_succeeded = False
    retry_count = self.request.retries
    try:
        with SessionLocal() as db:
            project = db.execute(
                select(Project).where(
                    Project.id == project_id,
                    Project.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if project is None:
                logger.info(
                    "project_risk_job_project_not_found",
                    extra={**context, "event_name": "project_risk_job_project_not_found"},
                )
                return {
                    "status": "not_found",
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                }

            if version_guard_enabled:
                completion_key = project_risk_job_completion_key(
                    tenant_id, project_id, expected_version
                )
                try:
                    current_version = cache.get_value(version_key)
                    version_read_succeeded = True
                    if current_version != expected_version:
                        logger.info(
                            "project_risk_job_superseded",
                            extra={**context, "event_name": "project_risk_job_superseded"},
                        )
                        return {
                            "status": "superseded",
                            "tenant_id": tenant_id,
                            "project_id": project_id,
                        }
                    if cache.get_value(completion_key) == expected_version:
                        logger.info(
                            "project_risk_job_duplicate",
                            extra={**context, "event_name": "project_risk_job_duplicate"},
                        )
                        return {
                            "status": "duplicate",
                            "tenant_id": tenant_id,
                            "project_id": project_id,
                        }
                    lock_acquired = cache.acquire_lock(
                        lock_key, lock_token, PROJECT_RISK_LOCK_TTL_SECONDS
                    )
                    if not lock_acquired:
                        cached_result = cache.get_json(cache_key)
                        if _cached_risk_matches_project(cached_result, project_id):
                            if mark_project_risk_job_complete_if_cached(
                                tenant_id,
                                project_id,
                                expected_version,
                            ):
                                logger.info(
                                    "project_risk_job_cache_already_current",
                                    extra={
                                        **context,
                                        "event_name": "project_risk_job_cache_already_current",
                                    },
                                )
                                return {
                                    "status": "duplicate",
                                    "tenant_id": tenant_id,
                                    "project_id": project_id,
                                }
                            current_version = cache.get_value(version_key)
                            if current_version != expected_version:
                                return {
                                    "status": "superseded",
                                    "tenant_id": tenant_id,
                                    "project_id": project_id,
                                }
                        if retry_count < MAX_RETRIES:
                            logger.warning(
                                "project_risk_job_lock_busy_retry",
                                extra={
                                    **context,
                                    "event_name": "project_risk_job_lock_busy_retry",
                                    "retry_count": retry_count,
                                },
                            )
                            raise self.retry(countdown=_retry_delay(retry_count))
                        logger.error(
                            "project_risk_job_lock_busy_deferred",
                            extra={
                                **context,
                                "event_name": "project_risk_job_lock_busy_deferred",
                                "retry_count": retry_count,
                            },
                        )
                        return {
                            "status": "deferred",
                            "tenant_id": tenant_id,
                            "project_id": project_id,
                        }
                    cached_result = cache.get_json(cache_key)
                    if _cached_risk_matches_project(cached_result, project_id):
                        if mark_project_risk_job_complete_if_cached(
                            tenant_id,
                            project_id,
                            expected_version,
                        ):
                            logger.info(
                                "project_risk_job_cache_already_current",
                                extra={
                                    **context,
                                    "event_name": "project_risk_job_cache_already_current",
                                },
                            )
                            return {
                                "status": "duplicate",
                                "tenant_id": tenant_id,
                                "project_id": project_id,
                            }
                        current_version = cache.get_value(version_key)
                        if current_version != expected_version:
                            return {
                                "status": "superseded",
                                "tenant_id": tenant_id,
                                "project_id": project_id,
                            }
                except (RedisError, OSError, TimeoutError) as exc:
                    logger.warning(
                        "project_risk_job_redis_retry",
                        extra={
                            **context,
                            "event_name": "project_risk_job_redis_retry",
                            "retry_count": retry_count,
                            "error_type": type(exc).__name__,
                        },
                    )
                    if retry_count < MAX_RETRIES:
                        raise self.retry(
                            exc=exc,
                            countdown=_retry_delay(retry_count),
                        )
                    logger.error(
                        "project_risk_job_redis_fallback",
                        extra={
                            **context,
                            "event_name": "project_risk_job_redis_fallback",
                            "retry_count": retry_count,
                        },
                    )

            result = analyze_project_risk(db, project)
            stored = False
            if version_read_succeeded:
                try:
                    stored = cache.set_json_if_version(
                        cache_key,
                        result,
                        PROJECT_RISK_CACHE_TTL_SECONDS,
                        version_key,
                        expected_version,
                        completion_key=(
                            project_risk_job_completion_key(
                                tenant_id, project_id, expected_version
                            )
                            if version_guard_enabled
                            else None
                        ),
                        completion_ttl_seconds=PROJECT_RISK_VERSION_TTL_SECONDS,
                    )
                except (RedisError, OSError, TimeoutError) as exc:
                    logger.warning(
                        "project_risk_job_cache_retry",
                        extra={
                            **context,
                            "event_name": "project_risk_job_cache_retry",
                            "retry_count": retry_count,
                            "error_type": type(exc).__name__,
                        },
                    )
                    if retry_count < MAX_RETRIES:
                        raise self.retry(
                            exc=exc,
                            countdown=_retry_delay(retry_count),
                        )
                    logger.error(
                        "project_risk_job_cache_fallback",
                        extra={
                            **context,
                            "event_name": "project_risk_job_cache_fallback",
                            "retry_count": retry_count,
                        },
                    )

            status = "updated" if stored else "calculated"
            logger.info(
                "project_risk_job_succeeded",
                extra={
                    **context,
                    "event_name": "project_risk_job_succeeded",
                    "result_status": status,
                    "risk_score": result["risk_score"],
                    "retry_count": retry_count,
                },
            )
            return {
                "status": status,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "risk_score": result["risk_score"],
            }
    except Retry:
        raise
    except OperationalError as exc:
        if retry_count < MAX_RETRIES:
            logger.warning(
                "project_risk_job_database_retry",
                extra={
                    **context,
                    "event_name": "project_risk_job_database_retry",
                    "retry_count": retry_count,
                },
            )
            raise self.retry(exc=exc, countdown=_retry_delay(retry_count))
        logger.error(
            "project_risk_job_database_failed",
            extra={
                **context,
                "event_name": "project_risk_job_database_failed",
                "retry_count": retry_count,
                "error_type": type(exc).__name__,
            },
        )
        raise
    except Exception as exc:
        logger.error(
            "project_risk_job_failed",
            extra={
                **context,
                "event_name": "project_risk_job_failed",
                "retry_count": retry_count,
                "error_type": type(exc).__name__,
            },
        )
        raise
    finally:
        if lock_acquired:
            try:
                cache.release_lock(lock_key, lock_token)
            except (RedisError, OSError, TimeoutError):
                logger.warning(
                    "project_risk_job_lock_release_failed",
                    extra={
                        **context,
                        "event_name": "project_risk_job_lock_release_failed",
                    },
                )


@celery_app.task(
    bind=True,
    name="backend.app.tasks.project_risk.schedule_periodic_checks",
    max_retries=MAX_RETRIES,
    soft_time_limit=270,
    time_limit=300,
    acks_late=True,
    reject_on_worker_lost=True,
)
def schedule_periodic_project_risk_checks(self) -> dict[str, int]:
    try:
        queued = 0
        with SessionLocal() as db:
            projects = db.execute(
                select(Project.tenant_id, Project.id).order_by(
                    Project.tenant_id, Project.id
                )
            ).yield_per(500)
            for tenant_id, project_id in projects:
                if enqueue_project_risk_recalculation(tenant_id, project_id) is not None:
                    queued += 1
        logger.info(
            "project_risk_periodic_scan_succeeded",
            extra={
                "event_name": "project_risk_periodic_scan_succeeded",
                "queued_count": queued,
                "retry_count": self.request.retries,
            },
        )
        return {"queued": queued}
    except Retry:
        raise
    except OperationalError as exc:
        if self.request.retries < MAX_RETRIES:
            logger.warning(
                "project_risk_periodic_scan_database_retry",
                extra={
                    "event_name": "project_risk_periodic_scan_database_retry",
                    "retry_count": self.request.retries,
                },
            )
            raise self.retry(
                exc=exc,
                countdown=_retry_delay(self.request.retries),
            )
        logger.error(
            "project_risk_periodic_scan_failed",
            extra={
                "event_name": "project_risk_periodic_scan_failed",
                "retry_count": self.request.retries,
                "error_type": type(exc).__name__,
            },
        )
        raise
    except Exception as exc:
        logger.error(
            "project_risk_periodic_scan_failed",
            extra={
                "event_name": "project_risk_periodic_scan_failed",
                "retry_count": self.request.retries,
                "error_type": type(exc).__name__,
            },
        )
        raise
