import json
from datetime import date, datetime
from typing import Any

from backend.app.infra.redis_client import get_redis_client

_INVALIDATE_JSON_SCRIPT = """
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('DEL', KEYS[2])
return 1
"""
_SET_JSON_IF_VERSION_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if ARGV[1] == '1' then
    if current ~= ARGV[2] then
        return 0
    end
elseif current then
    return 0
end
local written_version = redis.call('GET', KEYS[3])
local marker = ARGV[2]
if ARGV[1] == '0' then
    marker = '__missing_version__'
end
if written_version == marker and redis.call('EXISTS', KEYS[2]) == 1 then
    return 0
end
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
redis.call('SET', KEYS[3], marker, 'EX', ARGV[4])
if ARGV[5] == '1' then
    redis.call('SET', KEYS[4], ARGV[6], 'EX', ARGV[7])
end
return 1
"""
_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_MARK_JOB_COMPLETE_IF_CACHED_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
if redis.call('EXISTS', KEYS[2]) == 0 then
    return 0
end
redis.call('SET', KEYS[3], ARGV[1], 'EX', ARGV[2])
return 1
"""

def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def get_json(key: str) -> Any | None:
    cached_value = get_redis_client().get(key)
    if cached_value is None:
        return None
    return json.loads(cached_value)


def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    encoded = json.dumps(value, default=_json_default, separators=(",", ":"))
    get_redis_client().set(key, encoded, ex=ttl_seconds)


def delete_key(key: str) -> None:
    get_redis_client().delete(key)


def get_value(key: str) -> str | None:
    return get_redis_client().get(key)


def set_value(key: str, value: str, ttl_seconds: int) -> None:
    get_redis_client().set(key, value, ex=ttl_seconds)


def acquire_lock(key: str, token: str, ttl_seconds: int) -> bool:
    return bool(get_redis_client().set(key, token, ex=ttl_seconds, nx=True))


def release_lock(key: str, token: str) -> None:
    get_redis_client().eval(_RELEASE_LOCK_SCRIPT, 1, key, token)


def invalidate_json(
    key: str,
    version_key: str,
    version: str,
    version_ttl_seconds: int,
) -> None:
    get_redis_client().eval(
        _INVALIDATE_JSON_SCRIPT,
        2,
        version_key,
        key,
        version,
        version_ttl_seconds,
    )


def mark_job_complete_if_cached(
    cache_key: str,
    version_key: str,
    completion_key: str,
    expected_version: str,
    ttl_seconds: int,
) -> bool:
    return bool(
        get_redis_client().eval(
            _MARK_JOB_COMPLETE_IF_CACHED_SCRIPT,
            3,
            version_key,
            cache_key,
            completion_key,
            expected_version,
            ttl_seconds,
        )
    )


def set_json_if_version(
    key: str,
    value: Any,
    ttl_seconds: int,
    version_key: str,
    expected_version: str | None,
    completion_key: str | None = None,
    completion_ttl_seconds: int | None = None,
) -> bool:
    encoded = json.dumps(value, default=_json_default, separators=(",", ":"))
    has_expected_version = "1" if expected_version is not None else "0"
    return bool(
        get_redis_client().eval(
            _SET_JSON_IF_VERSION_SCRIPT,
            4,
            version_key,
            key,
            f"{key}:written-version",
            completion_key or f"{key}:unused-job-completion",
            has_expected_version,
            expected_version or "",
            encoded,
            ttl_seconds,
            "1" if completion_key is not None else "0",
            expected_version or "",
            completion_ttl_seconds or ttl_seconds,
        )
    )
