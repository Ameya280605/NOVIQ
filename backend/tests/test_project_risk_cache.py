import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "project-risk-cache-test-key"

from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError as KombuOperationalError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import Base, Project, Task, Tenant, User
from backend.app.services import project_risk_cache
from backend.app.services.project_risk_cache import (
    PROJECT_RISK_CACHE_TTL_SECONDS,
    project_risk_cache_key,
)
from backend.app.utils.jwt import create_access_token


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiry: dict[str, float] = {}
        self.set_ttls: list[int] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.eval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_get = False
        self.fail_set = False
        self.fail_delete = False

    def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        if self.fail_get:
            raise RedisConnectionError("Redis unavailable")
        if self.expiry.get(key, 0) <= time.monotonic():
            self.values.pop(key, None)
            self.expiry.pop(key, None)
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int) -> bool:
        if self.fail_set:
            raise RedisConnectionError("Redis unavailable")
        self.values[key] = value
        self.expiry[key] = time.monotonic() + ex
        self.set_ttls.append(ex)
        return True

    def delete(self, key: str) -> int:
        self.delete_calls.append(key)
        if self.fail_delete:
            raise RedisConnectionError("Redis unavailable")
        existed = key in self.values
        self.values.pop(key, None)
        self.expiry.pop(key, None)
        return int(existed)

    def eval(self, script: str, key_count: int, *args) -> int:
        self.eval_calls.append((script, args))
        keys = args[:key_count]
        values = args[key_count:]
        if "redis.call('DEL'" in script:
            version_key, cache_key = keys
            version, ttl = values
            self.values[version_key] = version
            self.expiry[version_key] = time.monotonic() + int(ttl)
            self.values.pop(cache_key, None)
            self.expiry.pop(cache_key, None)
            return 1

        version_key, cache_key, marker_key, completion_key = keys
        (
            has_expected,
            expected,
            encoded,
            ttl,
            completion_enabled,
            completion_value,
            completion_ttl,
        ) = values
        current_version = self.values.get(version_key)
        if self.expiry.get(version_key, 0) <= time.monotonic():
            current_version = None
            self.values.pop(version_key, None)
            self.expiry.pop(version_key, None)
        if has_expected == "1" and current_version != expected:
            return 0
        if has_expected == "0" and current_version is not None:
            return 0
        marker = expected if has_expected == "1" else "__missing_version__"
        cache_exists = (
            cache_key in self.values
            and self.expiry.get(cache_key, 0) > time.monotonic()
        )
        if self.values.get(marker_key) == marker and cache_exists:
            return 0
        self.values[cache_key] = encoded
        self.expiry[cache_key] = time.monotonic() + int(ttl)
        self.values[marker_key] = marker
        self.expiry[marker_key] = time.monotonic() + int(ttl)
        if completion_enabled == "1":
            self.values[completion_key] = completion_value
            self.expiry[completion_key] = (
                time.monotonic() + int(completion_ttl)
            )
        self.set_ttls.append(int(ttl))
        return 1


class ProjectRiskCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            tenant = Tenant(name="Cache Tenant")
            other_tenant = Tenant(name="Other Cache Tenant")
            db.add_all([tenant, other_tenant])
            db.flush()
            owner = User(
                tenant_id=tenant.id,
                name="Cache Owner",
                email="risk-cache-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            foreign_owner = User(
                tenant_id=other_tenant.id,
                name="Foreign Cache Owner",
                email="risk-cache-foreign-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            db.add_all([owner, foreign_owner])
            db.flush()
            project = Project(
                tenant_id=tenant.id,
                name="Cached project",
                status="active",
                created_at=now,
                updated_at=now,
            )
            foreign_project = Project(
                tenant_id=other_tenant.id,
                name="Foreign cached project",
                status="active",
                created_at=now,
                updated_at=now,
            )
            db.add_all([project, foreign_project])
            db.flush()
            task = Task(
                project_id=project.id,
                title="Cache test task",
                status="todo",
                priority="medium",
                created_at=now,
                updated_at=now,
            )
            db.add(task)
            db.commit()
            self.tenant_id = tenant.id
            self.other_tenant_id = other_tenant.id
            self.owner_id = owner.id
            self.foreign_owner_id = foreign_owner.id
            self.project_id = project.id
            self.foreign_project_id = foreign_project.id
            self.task_id = task.id

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.owner_headers = self._headers(self.owner_id, self.tenant_id, "owner")
        self.foreign_headers = self._headers(
            self.foreign_owner_id, self.other_tenant_id, "owner"
        )
        self.redis = FakeRedis()
        self.redis_patch = patch(
            "backend.app.services.cache.get_redis_client",
            return_value=self.redis,
        )
        self.redis_patch.start()
        self.enqueue_patch = patch(
            "backend.app.services.project_risk_jobs.enqueue_project_risk_recalculation",
            return_value="queued",
        )
        self.enqueue_mock = self.enqueue_patch.start()
        self.risk_calculation_patch = patch(
            "backend.app.services.project_risk_cache.analyze_project_risk",
            wraps=project_risk_cache.analyze_project_risk,
        )
        self.risk_calculation = self.risk_calculation_patch.start()

    def tearDown(self) -> None:
        self.risk_calculation_patch.stop()
        self.enqueue_patch.stop()
        self.redis_patch.stop()
        app.dependency_overrides.clear()
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        del _record
        connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _headers(user_id: int, tenant_id: int, role: str) -> dict[str, str]:
        token = create_access_token(
            {"sub": str(user_id), "tenant_id": tenant_id, "role": role}
        )
        return {"Authorization": f"Bearer {token}"}

    def _get_risk(self, project_id: int | None = None, headers=None):
        project_id = project_id or self.project_id
        return self.client.get(
            f"/projects/{project_id}/risk-analysis",
            headers=headers or self.owner_headers,
        )

    def test_cache_miss_then_hit_and_scoped_key(self) -> None:
        first = self._get_risk()
        second = self._get_risk()

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.risk_calculation.call_count, 1)
        key = project_risk_cache_key(self.tenant_id, self.project_id)
        self.assertIn(key, self.redis.values)
        self.assertEqual(
            self.redis.get_calls,
            [
                project_risk_cache.project_risk_version_key(
                    self.tenant_id, self.project_id
                ),
                key,
                project_risk_cache.project_risk_version_key(
                    self.tenant_id, self.project_id
                ),
                key,
            ],
        )
        self.assertEqual(
            project_risk_cache_key(self.tenant_id, self.project_id),
            project_risk_cache_key(
                self.tenant_id, self.project_id, project_risk_cache.SCORING_VERSION
            ),
        )
        self.assertNotEqual(
            key,
            project_risk_cache_key(self.other_tenant_id, self.project_id),
        )
        self.assertNotEqual(
            key,
            project_risk_cache_key(
                self.tenant_id, self.project_id, "next-version"
            ),
        )
        self.assertEqual(
            first.json()["generated_at"],
            second.json()["generated_at"],
        )

    def test_configured_ttl_is_applied_and_entries_expire(self) -> None:
        response = self._get_risk()
        self.assertEqual(response.status_code, 200, response.text)
        key = project_risk_cache_key(self.tenant_id, self.project_id)
        self.assertEqual(self.redis.set_ttls, [PROJECT_RISK_CACHE_TTL_SECONDS])
        self.assertGreater(self.redis.expiry[key], time.monotonic())

        self.redis.expiry[key] = time.monotonic() - 1
        expired = self._get_risk()
        self.assertEqual(expired.status_code, 200, expired.text)
        self.assertEqual(self.risk_calculation.call_count, 2)
        self.assertEqual(self.redis.set_ttls[-1], PROJECT_RISK_CACHE_TTL_SECONDS)

    def test_authorization_and_tenant_lookup_precede_cache_access(self) -> None:
        self.assertEqual(self._get_risk().status_code, 200)
        calls_before = len(self.redis.get_calls)
        unauthorized = self.client.get(
            f"/projects/{self.project_id}/risk-analysis"
        )
        cross_tenant = self._get_risk(headers=self.foreign_headers)
        self.assertIn(unauthorized.status_code, (401, 403))
        self.assertEqual(cross_tenant.status_code, 404)
        self.assertEqual(len(self.redis.get_calls), calls_before)

    def test_project_and_task_mutations_invalidate_after_commit(self) -> None:
        self.assertEqual(self._get_risk().status_code, 200)
        project_key = project_risk_cache_key(self.tenant_id, self.project_id)
        self.assertIn(project_key, self.redis.values)

        update_project = self.client.put(
            f"/projects/{self.project_id}",
            headers=self.owner_headers,
            json={"name": "Renamed cached project"},
        )
        self.assertEqual(update_project.status_code, 200, update_project.text)
        self.enqueue_mock.assert_called_once_with(self.tenant_id, self.project_id)
        self.enqueue_mock.reset_mock()
        self.assertNotIn(project_key, self.redis.values)
        after_project_update = self._get_risk()
        self.assertEqual(
            after_project_update.json()["project_name"], "Renamed cached project"
        )

        failed_update = self.client.put(
            f"/tasks/{self.task_id}",
            headers=self.owner_headers,
            json={"status": "completed"},
        )
        self.assertEqual(failed_update.status_code, 409)
        self.enqueue_mock.assert_not_called()
        self.assertIn(project_key, self.redis.values)

        score_before_task_update = after_project_update.json()["risk_score"]
        update_task = self.client.put(
            f"/tasks/{self.task_id}",
            headers=self.owner_headers,
            json={
                "status": "in_progress",
                "due_date": (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).isoformat(),
            },
        )
        self.assertEqual(update_task.status_code, 200, update_task.text)
        self.enqueue_mock.assert_called_once_with(self.tenant_id, self.project_id)
        self.enqueue_mock.reset_mock()
        self.assertNotIn(project_key, self.redis.values)
        after_task_update = self._get_risk()
        self.assertGreater(after_task_update.json()["risk_score"], score_before_task_update)

        create_task = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={"project_id": self.project_id, "title": "New cache invalidator"},
        )
        self.assertEqual(create_task.status_code, 200, create_task.text)
        self.enqueue_mock.assert_called_once_with(self.tenant_id, self.project_id)
        self.enqueue_mock.reset_mock()
        self.assertNotIn(project_key, self.redis.values)
        self.assertEqual(self._get_risk().status_code, 200)

        created_project = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={"name": "New cache project"},
        )
        self.assertEqual(created_project.status_code, 200, created_project.text)
        self.enqueue_mock.assert_called_once_with(
            self.tenant_id, created_project.json()["id"]
        )
        self.enqueue_mock.reset_mock()
        created_project_key = project_risk_cache_key(
            self.tenant_id, created_project.json()["id"]
        )
        self.assertTrue(
            any(
                "redis.call('DEL'" in script and created_project_key in args
                for script, args in self.redis.eval_calls
            )
        )

        created_task_id = create_task.json()["id"]
        delete_task = self.client.delete(
            f"/tasks/{created_task_id}", headers=self.owner_headers
        )
        self.assertEqual(delete_task.status_code, 200, delete_task.text)
        self.enqueue_mock.assert_called_once_with(self.tenant_id, self.project_id)
        self.enqueue_mock.reset_mock()
        self.assertNotIn(project_key, self.redis.values)
        self.assertEqual(self._get_risk().status_code, 200)

    def test_task_move_invalidates_both_projects(self) -> None:
        with self.session_factory() as db:
            destination = Project(
                tenant_id=self.tenant_id,
                name="Destination project",
                status="active",
            )
            db.add(destination)
            db.commit()
            db.refresh(destination)
            destination_id = destination.id

        self.assertEqual(self._get_risk(self.project_id).status_code, 200)
        self.assertEqual(self._get_risk(destination_id).status_code, 200)
        old_key = project_risk_cache_key(self.tenant_id, self.project_id)
        new_key = project_risk_cache_key(self.tenant_id, destination_id)

        response = self.client.put(
            f"/tasks/{self.task_id}",
            headers=self.owner_headers,
            json={"project_id": destination_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.enqueue_mock.call_count, 2)
        self.assertEqual(
            {call.args for call in self.enqueue_mock.call_args_list},
            {
                (self.tenant_id, self.project_id),
                (self.tenant_id, destination_id),
            },
        )
        self.assertNotIn(old_key, self.redis.values)
        self.assertNotIn(new_key, self.redis.values)

    def test_project_deletion_invalidates_cached_risk(self) -> None:
        with self.session_factory() as db:
            empty_project = Project(
                tenant_id=self.tenant_id,
                name="Empty removable project",
                status="planned",
            )
            db.add(empty_project)
            db.commit()
            db.refresh(empty_project)
            project_id = empty_project.id
        self.assertEqual(self._get_risk(project_id).status_code, 200)
        key = project_risk_cache_key(self.tenant_id, project_id)
        self.assertIn(key, self.redis.values)

        response = self.client.delete(
            f"/projects/{project_id}", headers=self.owner_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.enqueue_mock.assert_not_called()
        self.assertNotIn(key, self.redis.values)
        self.assertEqual(self._get_risk(project_id).status_code, 404)

    def test_redis_failure_falls_back_to_database_and_mutations_succeed(self) -> None:
        self.redis.fail_get = True
        self.redis.fail_set = True
        response = self._get_risk()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(response.json()["risk_score"], int)

        update_project = self.client.put(
            f"/projects/{self.project_id}",
            headers=self.owner_headers,
            json={"name": "Redis unavailable project update"},
        )
        self.assertEqual(update_project.status_code, 200, update_project.text)

        self.redis.fail_get = False
        self.redis.fail_set = False
        self.redis.fail_delete = True
        update_task = self.client.put(
            f"/tasks/{self.task_id}",
            headers=self.owner_headers,
            json={"priority": "high"},
        )
        self.assertEqual(update_task.status_code, 200, update_task.text)

    def test_broker_outage_does_not_undo_committed_project_update(self) -> None:
        self.enqueue_patch.stop()
        with patch(
            "backend.app.tasks.project_risk.recalculate_project_risk.apply_async",
            side_effect=KombuOperationalError("broker unavailable"),
        ):
            response = self.client.put(
                f"/projects/{self.project_id}",
                headers=self.owner_headers,
                json={"name": "Committed without broker"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        with self.session_factory() as db:
            project = db.query(Project).filter(Project.id == self.project_id).one()
            self.assertEqual(project.name, "Committed without broker")


if __name__ == "__main__":
    unittest.main()
