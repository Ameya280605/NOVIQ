import json
import logging
import os
from uuid import uuid4
from typing import Any

from dotenv import load_dotenv
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from backend.app.models import Project
from backend.app.services import cache
from backend.app.services.project_risk import SCORING_VERSION, analyze_project_risk

load_dotenv()

logger = logging.getLogger(__name__)
PROJECT_RISK_CACHE_TTL_SECONDS = int(
    os.getenv("PROJECT_RISK_CACHE_TTL_SECONDS", "60")
)
if PROJECT_RISK_CACHE_TTL_SECONDS < 1:
    raise ValueError("PROJECT_RISK_CACHE_TTL_SECONDS must be at least 1")
PROJECT_RISK_VERSION_TTL_SECONDS = 86400


def project_risk_cache_key(
    tenant_id: int,
    project_id: int,
    scoring_version: str = SCORING_VERSION,
) -> str:
    return (
        f"noviq:project-risk:tenant:{tenant_id}:project:{project_id}:"
        f"version:{scoring_version}"
    )


def project_risk_version_key(
    tenant_id: int,
    project_id: int,
    scoring_version: str = SCORING_VERSION,
) -> str:
    return f"{project_risk_cache_key(tenant_id, project_id, scoring_version)}:version"


def project_risk_job_completion_key(
    tenant_id: int,
    project_id: int,
    job_version: str,
    scoring_version: str = SCORING_VERSION,
) -> str:
    return (
        f"{project_risk_cache_key(tenant_id, project_id, scoring_version)}:"
        f"job-complete:{job_version}"
    )


def mark_project_risk_job_complete_if_cached(
    tenant_id: int,
    project_id: int,
    job_version: str,
) -> bool:
    return cache.mark_job_complete_if_cached(
        project_risk_cache_key(tenant_id, project_id),
        project_risk_version_key(tenant_id, project_id),
        project_risk_job_completion_key(tenant_id, project_id, job_version),
        job_version,
        PROJECT_RISK_VERSION_TTL_SECONDS,
    )


def get_project_risk(
    db: Session,
    project: Project,
) -> dict[str, Any]:
    key = project_risk_cache_key(project.tenant_id, project.id)
    version_key = project_risk_version_key(project.tenant_id, project.id)
    expected_version: str | None = None
    version_read_succeeded = False
    try:
        expected_version = cache.get_value(version_key)
        version_read_succeeded = True
        cached_result = cache.get_json(key)
        if isinstance(cached_result, dict):
            return cached_result
        if cached_result is not None:
            logger.warning("project_risk_cache_invalid_payload")
    except (RedisError, OSError, TimeoutError, json.JSONDecodeError):
        logger.warning("project_risk_cache_read_failed")

    result = analyze_project_risk(db, project)
    if version_read_succeeded:
        try:
            cache.set_json_if_version(
                key,
                result,
                PROJECT_RISK_CACHE_TTL_SECONDS,
                version_key,
                expected_version,
            )
        except (RedisError, OSError, TimeoutError):
            logger.warning("project_risk_cache_write_failed")
    return result


def invalidate_project_risk(
    tenant_id: int,
    project_ids: set[int] | list[int] | tuple[int, ...],
) -> None:
    for project_id in set(project_ids):
        key = project_risk_cache_key(tenant_id, project_id)
        try:
            cache.invalidate_json(
                key,
                project_risk_version_key(tenant_id, project_id),
                str(uuid4()),
                PROJECT_RISK_VERSION_TTL_SECONDS,
            )
        except (RedisError, OSError, TimeoutError):
            logger.warning("project_risk_cache_invalidation_failed")
