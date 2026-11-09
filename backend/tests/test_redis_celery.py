import os
import unittest
from unittest.mock import Mock, patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "redis-celery-test-signing-key"

from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from backend.app.celery_app import BROKER_URL, RESULT_BACKEND, celery_app
from backend.app.infra import redis_client
from backend.app.main import app
from backend.app.tasks.health import echo_task


class RedisCeleryFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        redis_client.close_redis_client()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        redis_client.close_redis_client()

    def test_redis_client_is_lazy_cached_and_closed(self) -> None:
        fake_client = Mock()
        with patch.object(redis_client.Redis, "from_url", return_value=fake_client) as factory:
            first = redis_client.get_redis_client()
            second = redis_client.get_redis_client()

            self.assertIs(first, second)
            factory.assert_called_once_with(
                redis_client.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
                health_check_interval=30,
            )
            redis_client.close_redis_client()

        fake_client.close.assert_called_once_with()
        self.assertIsNone(redis_client._client)

    def test_celery_uses_redis_urls_and_json_serialization(self) -> None:
        self.assertEqual(BROKER_URL, os.getenv("CELERY_BROKER_URL") or redis_client.REDIS_URL)
        self.assertEqual(
            RESULT_BACKEND,
            os.getenv("CELERY_RESULT_BACKEND") or redis_client.REDIS_URL,
        )
        self.assertEqual(celery_app.conf.broker_url, BROKER_URL)
        self.assertEqual(celery_app.conf.result_backend, RESULT_BACKEND)
        self.assertEqual(celery_app.conf.accept_content, ["json"])
        self.assertEqual(celery_app.conf.task_serializer, "json")
        self.assertTrue(celery_app.conf.enable_utc)
        self.assertTrue(celery_app.conf.task_publish_retry)
        self.assertEqual(
            celery_app.conf.task_publish_retry_policy["max_retries"], 3
        )
        self.assertEqual(celery_app.conf.worker_prefetch_multiplier, 1)
        self.assertTrue(celery_app.conf.task_acks_late)
        self.assertTrue(celery_app.conf.task_reject_on_worker_lost)

    def test_echo_task_executes_in_eager_test_mode(self) -> None:
        was_eager = celery_app.conf.task_always_eager
        was_propagating = celery_app.conf.task_eager_propagates
        celery_app.conf.update(
            task_always_eager=True,
            task_eager_propagates=True,
        )
        try:
            result = echo_task.apply_async(args=["phase-1-test"])
        finally:
            celery_app.conf.update(
                task_always_eager=was_eager,
                task_eager_propagates=was_propagating,
            )

        self.assertTrue(result.successful())
        self.assertEqual(
            result.get(timeout=1),
            {"status": "ok", "message": "phase-1-test"},
        )

    def test_redis_health_check_success_and_unavailable(self) -> None:
        healthy_client = Mock()
        with patch(
            "backend.app.routes.health_routes.get_redis_client",
            return_value=healthy_client,
        ):
            response = self.client.get("/health/redis")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        healthy_client.ping.assert_called_once_with()

        unavailable_client = Mock()
        unavailable_client.ping.side_effect = RedisConnectionError("not reachable")
        with patch(
            "backend.app.routes.health_routes.get_redis_client",
            return_value=unavailable_client,
        ):
            response = self.client.get("/health/redis")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], {"status": "unavailable"})

    def test_worker_health_check_success_and_unavailable(self) -> None:
        with patch.object(
            celery_app.control, "ping", return_value=[{"worker@host": {"ok": "pong"}}]
        ):
            response = self.client.get("/health/worker")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        with patch.object(celery_app.control, "ping", return_value=[]):
            response = self.client.get("/health/worker")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], {"status": "unavailable"})


if __name__ == "__main__":
    unittest.main()
