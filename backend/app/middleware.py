import logging
import re
from time import perf_counter
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from backend.app.metrics import record_http_request


logger = logging.getLogger(__name__)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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
