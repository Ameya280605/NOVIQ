import os
import time
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "rate-limit-test-signing-key-123456"

from fastapi import APIRouter
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from backend.app.main import app
from backend.app.utils.jwt import create_access_token
from backend.app.metrics import reset_metrics
from backend.app.services.rate_limit import check_rate_limit


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expiry: dict[str, float] = {}
        self.windows: dict[str, int] = {}
        self.now = time.monotonic()

    def eval(self, _script, _numkeys, key, window_seconds):
        if self.expiry.get(key, 0) <= self.now:
            self.counts.pop(key, None)
            self.expiry.pop(key, None)
            self.windows.pop(key, None)
        if key not in self.counts:
            self.counts[key] = 0
            self.windows[key] = int(window_seconds)
            self.expiry[key] = self.now + int(window_seconds)
        self.counts[key] += 1
        ttl = max(1, int(self.expiry[key] - self.now))
        return [self.counts[key], ttl]

    def advance(self, seconds: float) -> None:
        self.now += seconds


rate_router = APIRouter()


@rate_router.get("/_test/rate-limit")
def regular_rate_limit():
    return {"status": "ok"}


app.include_router(rate_router)


def _headers(tenant_id: int) -> dict[str, str]:
    token = create_access_token(
        {"sub": "1", "tenant_id": tenant_id, "role": "member"}
    )
    return {"Authorization": f"Bearer {token}"}


def test_regular_api_rate_limit_is_enforced(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("RATE_LIMIT_API_REQUESTS", "2")
    monkeypatch.setenv("RATE_LIMIT_API_WINDOW_SECONDS", "60")

    with patch(
        "backend.app.services.rate_limit.get_redis_client",
        return_value=fake,
    ):
        client = TestClient(app)
        first = client.get(
            "/_test/rate-limit",
            headers=_headers(tenant_id=1),
        )
        second = client.get(
            "/_test/rate-limit",
            headers=_headers(tenant_id=1),
        )
        limited = client.get(
            "/_test/rate-limit",
            headers=_headers(tenant_id=1),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()
    assert limited.json() == {"detail": "Too many requests"}


def test_rate_limit_isolated_by_tenant(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("RATE_LIMIT_API_REQUESTS", "1")
    monkeypatch.setenv("RATE_LIMIT_API_WINDOW_SECONDS", "60")

    with patch(
        "backend.app.services.rate_limit.get_redis_client",
        return_value=fake,
    ):
        client = TestClient(app)
        tenant_one = client.get(
            "/_test/rate-limit",
            headers=_headers(tenant_id=1),
        )
        tenant_one_limited = client.get(
            "/_test/rate-limit",
            headers=_headers(tenant_id=1),
        )
        tenant_two = client.get(
            "/_test/rate-limit",
            headers=_headers(tenant_id=2),
        )

    assert tenant_one.status_code == 200
    assert tenant_one_limited.status_code == 429
    assert tenant_two.status_code == 200


def test_auth_endpoint_has_stricter_limit(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("RATE_LIMIT_AUTH_REQUESTS", "1")
    monkeypatch.setenv("RATE_LIMIT_AUTH_WINDOW_SECONDS", "30")

    with patch(
        "backend.app.services.rate_limit.get_redis_client",
        return_value=fake,
    ):
        client = TestClient(app)
        first = client.post(
            "/users/login",
            json={"email": "not-an-email", "password": "password"},
        )
        limited = client.post(
            "/users/login",
            json={"email": "not-an-email", "password": "password"},
        )

    assert first.status_code == 422
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()


def test_redis_failure_fails_open(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_API_REQUESTS", "1")

    with patch(
        "backend.app.services.rate_limit.get_redis_client",
        side_effect=RedisError("redis unavailable"),
    ):
        response = TestClient(app).get(
            "/_test/rate-limit",
            headers=_headers(tenant_id=10),
        )

    assert response.status_code == 200


def test_rate_limit_window_expiry(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("RATE_LIMIT_API_REQUESTS", "1")
    monkeypatch.setenv("RATE_LIMIT_API_WINDOW_SECONDS", "10")

    with patch(
        "backend.app.services.rate_limit.get_redis_client",
        return_value=fake,
    ):
        client = TestClient(app)
        headers = _headers(tenant_id=20)
        first = client.get("/_test/rate-limit", headers=headers)
        limited = client.get("/_test/rate-limit", headers=headers)
        fake.advance(11)
        after_expiry = client.get("/_test/rate-limit", headers=headers)

    assert first.status_code == 200
    assert limited.status_code == 429
    assert after_expiry.status_code == 200


def test_missing_redis_expiry_is_restored_and_rate_limit_still_works():
    class MissingExpiryRedis:
        def __init__(self) -> None:
            self.count = 0
            self.ttl = -1
            self.expiry_restored = False

        def eval(self, script, _numkeys, key, window_seconds):
            del script, key
            self.count += 1
            if self.ttl < 0:
                self.expiry_restored = True
                self.ttl = int(window_seconds)
            return [self.count, self.ttl]

        def get_ttl(self) -> int:
            return self.ttl

    redis = MissingExpiryRedis()

    with patch(
        "backend.app.services.rate_limit.get_redis_client",
        return_value=redis,
    ):
        first = check_rate_limit("noviq:test", 1, 30)
        limited = check_rate_limit("noviq:test", 1, 30)

    assert redis.expiry_restored is True
    assert redis.get_ttl() > 0
    assert first.allowed is True
    assert limited.allowed is False
    assert limited.retry_after_seconds >= 1


def test_unexpected_limiter_failure_fails_open_and_preserves_request_handling(monkeypatch, caplog):
    monkeypatch.setenv("RATE_LIMIT_API_REQUESTS", "1")
    reset_metrics()

    with patch(
        "backend.app.middleware.check_rate_limit",
        side_effect=RuntimeError("unexpected limiter failure"),
    ):
        client = TestClient(app)
        caplog.set_level("INFO", logger="backend.app.middleware")
        response = client.get(
            "/_test/rate-limit",
            headers=_headers(tenant_id=30),
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]
    matching_logs = [
        record for record in caplog.records
        if getattr(record, "path", None) == "/_test/rate-limit"
    ]
    assert matching_logs
    assert matching_logs[-1].status_code == 200
    assert matching_logs[-1].outcome == "ok"

    metrics_body = client.get("/metrics").text
    assert 'route="/_test/rate-limit"' in metrics_body
