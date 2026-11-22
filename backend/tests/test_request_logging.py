import os
import re

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "request-logging-test-signing-key"

from fastapi.testclient import TestClient
from fastapi import APIRouter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import Base, Tenant, User
from backend.app.utils.jwt import create_access_token
from backend.app import middleware


regression_router = APIRouter()


@regression_router.get("/_test/unhandled-error")
def unhandled_error():
    raise RuntimeError("forced unhandled error")


app.include_router(regression_router)


def test_request_id_is_generated_and_logged(caplog):
    client = TestClient(app)
    caplog.set_level("INFO", logger=middleware.logger.name)

    response = client.get("/")

    request_id = response.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    record = next(
        record for record in caplog.records if record.name == middleware.logger.name
    )
    assert record.request_id == request_id
    assert record.http_method == "GET"
    assert record.path == "/"
    assert record.status_code == 200
    assert record.duration_ms >= 0


def test_request_id_is_propagated_from_safe_incoming_header():
    client = TestClient(app)

    response = client.get("/", headers={"X-Request-ID": "client-request-123"})

    assert response.headers["X-Request-ID"] == "client-request-123"


def test_malformed_incoming_request_id_is_replaced():
    client = TestClient(app)

    response = client.get("/", headers={"X-Request-ID": "invalid id\n"})

    request_id = response.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)


def test_unhandled_error_has_request_id_and_is_logged(caplog):
    client = TestClient(app, raise_server_exceptions=False)
    caplog.set_level("INFO", logger=middleware.logger.name)

    response = client.get("/_test/unhandled-error")

    request_id = response.headers["X-Request-ID"]
    assert response.status_code == 500
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    record = next(
        record
        for record in reversed(caplog.records)
        if record.name == middleware.logger.name
        and record.path == "/_test/unhandled-error"
    )
    assert record.request_id == request_id
    assert record.status_code == 500
    assert record.outcome == "error"


def test_authenticated_context_is_logged_without_sensitive_data(caplog):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        tenant = Tenant(name="Logging Tenant")
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id,
            name="Logging User",
            email="logging@example.test",
            password_hash="not-a-real-hash",
            role="member",
        )
        db.add(user)
        db.commit()
        tenant_id = tenant.id
        user_id = user.id

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    caplog.set_level("INFO", logger=middleware.logger.name)
    token = create_access_token(
        {"sub": str(user_id), "tenant_id": tenant_id, "role": "member"}
    )
    password = "do-not-log-this-password"
    authorization = "Bearer do-not-log-this-token"
    try:
        response = TestClient(app).get(
            "/users/members",
            headers={
                "Authorization": authorization,
                "X-Test-Password": password,
            },
        )
        assert response.status_code == 401

        response = TestClient(app).get(
            "/users/members",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        record = next(
            record
            for record in reversed(caplog.records)
            if record.name == middleware.logger.name
            and record.path == "/users/members"
        )
        assert record.tenant_id == tenant_id
        assert record.user_id == user_id
        assert password not in caplog.text
        assert authorization not in caplog.text
        assert token not in caplog.text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_logging_failure_does_not_break_request(monkeypatch):
    monkeypatch.setattr(
        middleware.logger,
        "info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("logging failed")),
    )

    response = TestClient(app).get("/")

    assert response.status_code == 200
