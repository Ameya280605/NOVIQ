import logging
import re
from hashlib import sha256
from time import perf_counter
import time
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from jose import jwt

from backend.app.metrics import record_http_request
from backend.app.services.rate_limit import (
    check_rate_limit,
    rate_limit_config,
    RateLimitResult,
)


logger = logging.getLogger(__name__)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_AUTH_PATHS = frozenset({
    "/users/login",
    "/users/register",
})


def _request_id(request: Request) -> str:
    provided = request.headers.get("X-Request-ID")
    if provided and _REQUEST_ID_PATTERN.fullmatch(provided):
        return provided
    return uuid4().hex


def _safe_log(fields: dict[str, object]) -> None:
    try:
        logger.info("api_request", extra=fields)
    except Exception:
        # Logging must never change the request outcome.
        return


def _metric_route(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    if request.url.path == "/metrics":
        return "/metrics"
    return "unmatched"


def _safe_metric(
    request: Request,
    status_code: int,
    duration_seconds: float,
) -> None:
    try:
        record_http_request(
            request.method,
            _metric_route(request),
            status_code,
            duration_seconds,
        )
    except Exception:
        return


def _client_identity(request: Request) -> str:
    client_host = request.client.host if request.client else "unknown"
    return sha256(client_host.encode("utf-8")).hexdigest()[:16]


def _tenant_identity(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.lower().startswith("bearer "):
        return _client_identity(request)
    token = authorization.split(None, 1)[1].strip()
    if not token:
        return _client_identity(request)
    try:
        from backend.app.utils.jwt import ALGORITHM, SECRET_KEY

        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )
        tenant_id = payload.get("tenant_id")
        if tenant_id is not None:
            request.state.tenant_id = tenant_id
            request.state.user_id = payload.get("sub")
            return f"tenant:{tenant_id}"
    except Exception:
        pass
    return _client_identity(request)


def _is_api_request(path: str) -> bool:
    if path in _AUTH_PATHS:
        return True
    if path == "/metrics" or path.startswith("/health") or path == "/":
        return False
    if path.startswith("/docs") or path.startswith("/openapi"):
        return False
    return True


def _rate_limit_response(request: Request, request_id: str, retry_after: int) -> Response:
    response = JSONResponse(
        status_code=429,
        content={"detail": "Too many requests"},
        headers={
            "X-Request-ID": request_id,
            "Retry-After": str(max(1, retry_after)),
        },
    )
    return response


async def request_logging_middleware(
    request: Request,
    call_next,
) -> Response:
    request_id = _request_id(request)
    request.state.request_id = request_id
    started_at = perf_counter()
    response: Response | None = None
    outcome = "ok"
    status_code = 500

    if _is_api_request(request.url.path):
        try:
            config = rate_limit_config()
            if request.url.path in _AUTH_PATHS:
                identity = _client_identity(request)
                limit = config.auth_requests
                window = config.auth_window_seconds
                scope = "auth"
            else:
                identity = _tenant_identity(request)
                limit = config.api_requests
                window = config.api_window_seconds
                scope = "api"
            window_bucket = int(time.time()) // window
            key = f"noviq:rate_limit:{scope}:{identity}:{window_bucket}"
            rate_result = check_rate_limit(key, limit, window)
        except Exception:
            rate_result = RateLimitResult(
                allowed=True,
                retry_after_seconds=0,
            )
        if not rate_result.allowed:
            status_code = 429
            outcome = "error"
            duration_seconds = perf_counter() - started_at
            response = _rate_limit_response(
                request,
                request_id,
                rate_result.retry_after_seconds,
            )
            _safe_metric(request, status_code, duration_seconds)
            fields = {
                "request_id": request_id,
                "http_method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": round(duration_seconds * 1000, 3),
                "outcome": outcome,
            }
            tenant_id = getattr(request.state, "tenant_id", None)
            user_id = getattr(request.state, "user_id", None)
            if tenant_id is not None:
                fields["tenant_id"] = tenant_id
            if user_id is not None:
                fields["user_id"] = user_id
            _safe_log(fields)
            return response

    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception:
        outcome = "error"
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
            headers={"X-Request-ID": request_id},
        )
        return response
    finally:
        duration_seconds = perf_counter() - started_at
        if response is not None:
            response.headers["X-Request-ID"] = request_id
        _safe_metric(request, status_code, duration_seconds)

        fields: dict[str, object] = {
            "request_id": request_id,
            "http_method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": round((perf_counter() - started_at) * 1000, 3),
            "outcome": outcome,
        }
        tenant_id = getattr(request.state, "tenant_id", None)
        user_id = getattr(request.state, "user_id", None)
        if tenant_id is not None:
            fields["tenant_id"] = tenant_id
        if user_id is not None:
            fields["user_id"] = user_id
        _safe_log(fields)
