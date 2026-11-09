import logging

from celery.exceptions import CeleryError
from fastapi import APIRouter, HTTPException, status
from kombu.exceptions import OperationalError
from redis.exceptions import RedisError

from backend.app.celery_app import celery_app
from backend.app.infra.redis_client import get_redis_client

router = APIRouter(prefix="/health", tags=["Health"])
logger = logging.getLogger(__name__)


@router.get("/redis")
def redis_health():
    try:
        get_redis_client().ping()
    except (RedisError, OSError, TimeoutError) as exc:
        logger.warning("redis_health_check_failed", extra={"component": "redis"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unavailable"},
        ) from exc
    return {"status": "ok"}


@router.get("/worker")
def worker_health():
    try:
        replies = celery_app.control.ping(timeout=1.0)
    except (CeleryError, OperationalError, RedisError, OSError, TimeoutError) as exc:
        logger.warning("celery_worker_health_check_failed", extra={"component": "worker"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unavailable"},
        ) from exc
    if not replies:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unavailable"},
        )
    return {"status": "ok"}
