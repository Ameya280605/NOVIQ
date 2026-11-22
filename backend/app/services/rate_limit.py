import os
from dataclasses import dataclass

from backend.app.infra.redis_client import get_redis_client


@dataclass(frozen=True)
class RateLimitConfig:
    auth_requests: int
    auth_window_seconds: int
    api_requests: int
    api_window_seconds: int


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def rate_limit_config() -> RateLimitConfig:
    return RateLimitConfig(
        auth_requests=_positive_int("RATE_LIMIT_AUTH_REQUESTS", 10),
        auth_window_seconds=_positive_int(
            "RATE_LIMIT_AUTH_WINDOW_SECONDS",
            60,
        ),
        api_requests=_positive_int("RATE_LIMIT_API_REQUESTS", 1000),
        api_window_seconds=_positive_int(
            "RATE_LIMIT_API_WINDOW_SECONDS",
            60,
        ),
    )


def check_rate_limit(
    key: str,
    limit: int,
    window_seconds: int,
) -> RateLimitResult:
    try:
        result = get_redis_client().eval(
            _LIMIT_SCRIPT,
            1,
            key,
            window_seconds,
        )
        count = int(result[0])
        ttl = max(1, int(result[1]))
        if count > limit:
            return RateLimitResult(False, ttl)
        return RateLimitResult(True, ttl)
    except Exception:
        # Redis outages must not take the API offline.
        return RateLimitResult(True, 0)
