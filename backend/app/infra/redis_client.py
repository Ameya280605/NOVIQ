import os
from threading import Lock

from dotenv import load_dotenv
from redis import Redis

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_client: Redis | None = None
_client_lock = Lock()


def get_redis_client() -> Redis:
    global _client
    with _client_lock:
        if _client is None:
            _client = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
                health_check_interval=30,
            )
        return _client


def close_redis_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None
