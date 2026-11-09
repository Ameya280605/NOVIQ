import os

from celery import Celery
from celery.schedules import crontab

from backend.app.infra.redis_client import REDIS_URL

BROKER_URL = os.getenv("CELERY_BROKER_URL") or REDIS_URL
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND") or REDIS_URL

celery_app = Celery(
    "noviq",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=[
        "backend.app.tasks.health",
        "backend.app.tasks.project_risk",
    ],
)

celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    enable_utc=True,
    timezone="UTC",
    task_track_started=True,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=3,
    task_publish_retry=True,
    task_publish_retry_policy={
        "max_retries": 3,
        "interval_start": 0,
        "interval_step": 0.2,
        "interval_max": 1,
    },
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    beat_schedule={
        "schedule-project-risk-checks-every-15-minutes": {
            "task": "backend.app.tasks.project_risk.schedule_periodic_checks",
            "schedule": crontab(minute="*/15"),
        },
    },
)
