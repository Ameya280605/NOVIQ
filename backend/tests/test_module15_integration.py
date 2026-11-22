import os
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "module15-integration-signing-key-123456"

from fastapi import APIRouter
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from backend.app.main import app
from backend.app.metrics import reset_metrics
from backend.app.utils import jwt
from backend.app.utils.jwt import create_access_token


integration_router = APIRouter()


@integration_router.get("/_integration/items/{item_id}")
def integration_item(item_id: int):
    return {"item_id": item_id}


@integration_router.get("/_integration/error")
def integration_error():
    raise RuntimeError("integration forced error")


app.include_router(integration_router)


class IntegrationFakeRedis:
    def __init__(self) -> None:
        self.counts = {}

    def eval(self, _script, _numkeys, key, window_seconds):
        del window_seconds
        self.counts[key] = self.counts.get(key, 0) + 1
        return [self.counts[key], 30]


def test_request_id_metrics_logging_and_429_integration(monkeypatch, caplog):
    reset_metrics()
    monkeypatch.setenv("RATE_LIMIT_API_REQUESTS", "1")
    monkeypatch.setenv("RATE_LIMIT_API_WINDOW_SECONDS", "60")
    fake = IntegrationFakeRedis()
    token = create_access_token(
        {"sub": "42", "tenant_id": 7, "role": "member"}
    )

    with patch(
        "backend.app.services.rate_limit.get_redis_client",
        return_value=fake,
    ):
        client = TestClient(app)
        caplog.set_level("INFO", logger="backend.app.middleware")
        first = client.get(
            "/_integration/items/123",
            headers={"Authorization": f"Bearer {token}"},
        )
        limited = client.get(
            "/_integration/items/123",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()
    assert limited.headers["X-Request-ID"]
    limited_log = next(
        record
        for record in reversed(caplog.records)
        if getattr(record, "status_code", None) == 429
    )
    assert limited_log.request_id == limited.headers["X-Request-ID"]
    assert limited_log.outcome == "error"
    assert limited_log.tenant_id == 7
    assert limited_log.user_id == "42"

    metrics = client.get("/metrics").text
    assert 'route="/_integration/items/{item_id}"' in metrics
    assert "/_integration/items/123" not in metrics
    assert 'status="429"' in metrics


def test_unhandled_error_request_id_logging_and_metrics_integration(caplog):
    reset_metrics()
    client = TestClient(app, raise_server_exceptions=False)
    caplog.set_level("INFO", logger="backend.app.middleware")

    response = client.get("/_integration/error")

    assert response.status_code == 500
    assert response.headers["X-Request-ID"]
    record = next(
        record
        for record in reversed(caplog.records)
        if getattr(record, "status_code", None) == 500
    )
    assert record.request_id == response.headers["X-Request-ID"]
    assert record.outcome == "error"

    metrics = client.get("/metrics").text
    assert 'noviq_http_requests_total{method="GET",route="/_integration/error",status="500"} 1' in metrics
    assert 'noviq_http_request_errors_total{method="GET",route="/_integration/error",status_class="5xx"} 1' in metrics


def test_redis_failure_allows_request_and_preserves_request_id(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_API_REQUESTS", "1")
    token = create_access_token(
        {"sub": "43", "tenant_id": 8, "role": "member"}
    )

    with patch(
        "backend.app.services.rate_limit.get_redis_client",
        side_effect=RedisError("redis unavailable"),
    ):
        response = TestClient(app).get(
            "/_integration/items/456",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_invalid_or_short_jwt_secret_fails_startup():
    with patch.object(jwt, "SECRET_KEY", "too-short"):
        try:
            jwt.validate_jwt_secret()
        except RuntimeError:
            pass
        else:
            raise AssertionError("JWT startup validation did not fail")


def test_health_and_metrics_endpoints_preserve_safe_response_shapes():
    client = TestClient(app)

    live = client.get("/health/live")
    metrics = client.get("/metrics")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert (
        metrics.headers["content-type"]
        == "text/plain; version=0.0.4; charset=utf-8"
    )
    assert "Authorization" not in metrics.text


def test_readiness_failure_returns_safe_503_body():
    with patch(
        "backend.app.routes.health_routes.get_redis_client",
        side_effect=RedisError("redis unavailable"),
    ):
        response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": {"status": "unavailable"}}
