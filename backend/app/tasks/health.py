from backend.app.celery_app import celery_app


@celery_app.task(name="backend.app.tasks.health.echo")
def echo_task(message: str) -> dict[str, str]:
    return {"status": "ok", "message": message}
