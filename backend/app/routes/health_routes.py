import logging

from celery.exceptions import CeleryError
from fastapi import APIRouter, HTTPException, status
from kombu.exceptions import OperationalError
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from backend.app.celery_app import celery_app
from backend.app.database import engine
from backend.app.infra.redis_client import get_redis_client

router = APIRouter(prefix="/health", tags=["Health"])
logger = logging.getLogger(__name__)


@router.get("/live")
def liveness_health():
    return {"status": "ok"}


@router.get("/ready")
def readiness_health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        get_redis_client().ping()
        replies = celery_app.control.ping(timeout=1.0)
        if not replies:
            raise RuntimeError("No Celery workers responded")
    except (
        CeleryError,
        OperationalError,
        RedisError,
        OSError,
        SQLAlchemyError,
        TimeoutError,
        RuntimeError,
    ):
        logger.warning(
            "service_readiness_check_failed",
            extra={"component": "dependencies"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unavailable"},
        )
    return {"status": "ok"}


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
