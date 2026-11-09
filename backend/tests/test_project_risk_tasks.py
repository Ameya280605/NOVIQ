import os
import logging
import json
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "project-risk-task-test-signing-key"

from celery.schedules import crontab
from kombu.exceptions import OperationalError as KombuOperationalError
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.celery_app import celery_app
from backend.app.models import Base, Project, Task, Tenant
from backend.app.services import cache
from backend.app.services import project_risk_cache
from backend.app.services.project_risk_jobs import (
    enqueue_project_risk_recalculation,
)
from backend.app.services.project_risk_cache import (
    PROJECT_RISK_CACHE_TTL_SECONDS,
    project_risk_job_completion_key,
    project_risk_cache_key,
    project_risk_version_key,
)
from backend.app.tasks import project_risk as project_risk_tasks


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiry: dict[str, float] = {}
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, str, int]] = []
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.fail_get = False
        self.fail_eval_count = 0

    def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        if self.fail_get:
            raise ConnectionError("Redis unavailable")
        if self.expiry.get(key, 0) <= time.monotonic():
            self.values.pop(key, None)
            self.expiry.pop(key, None)
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int, nx: bool = False) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expiry[key] = time.monotonic() + ex
        self.set_calls.append((key, value, ex))
        return True

    def eval(self, script: str, key_count: int, *args) -> int:
        self.eval_calls.append((script, key_count, args))
        if self.fail_eval_count:
            self.fail_eval_count -= 1
            raise ConnectionError("temporary Redis script failure")
        if key_count == 1:
            lock_key = args[0]
            token = args[1]
            if self.values.get(lock_key) == token:
                self.values.pop(lock_key, None)
                self.expiry.pop(lock_key, None)
                return 1
            return 0
        if "if redis.call('GET', KEYS[1]) ~= ARGV[1]" in script:
            version_key, result_key, completion_key = args[:key_count]
            expected_version, ttl = args[key_count:]
            if self.values.get(version_key) != expected_version:
                return 0
            if result_key not in self.values:
                return 0
            self.values[completion_key] = expected_version
            self.expiry[completion_key] = time.monotonic() + int(ttl)
            return 1
        if key_count == 2:
            version_key, result_key = args[:key_count]
            version, ttl = args[key_count:]
            self.values[version_key] = version
            self.expiry[version_key] = time.monotonic() + int(ttl)
            self.values.pop(result_key, None)
            self.expiry.pop(result_key, None)
            return 1

        version_key, result_key, marker_key, completion_key = args[:key_count]
        (
            has_expected,
            expected,
            encoded,
            ttl,
            completion_enabled,
            completion_value,
            completion_ttl,
        ) = args[key_count:]
        current = self.values.get(version_key)
        if self.expiry.get(version_key, 0) <= time.monotonic():
            current = None
        if has_expected == "1" and current != expected:
            return 0
        if has_expected == "0" and current is not None:
            return 0
        marker = expected if has_expected == "1" else "__missing_version__"
        cache_exists = (
            result_key in self.values
            and self.expiry.get(result_key, 0) > time.monotonic()
        )
        if self.values.get(marker_key) == marker and cache_exists:
            return 0
        self.set(result_key, encoded, int(ttl))
        self.values[marker_key] = marker
        self.expiry[marker_key] = time.monotonic() + int(ttl)
        if completion_enabled == "1":
            self.values[completion_key] = completion_value
            self.expiry[completion_key] = (
                time.monotonic() + int(completion_ttl)
            )
        return 1


class ProjectRiskTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        with self.session_factory() as db:
            tenant = Tenant(name="Background Risk Tenant")
            other_tenant = Tenant(name="Other Background Risk Tenant")
            db.add_all([tenant, other_tenant])
            db.flush()
            project = Project(
                tenant_id=tenant.id,
                name="Background Project",
                status="active",
            )
            foreign_project = Project(
                tenant_id=other_tenant.id,
                name="Foreign Background Project",
                status="active",
            )
            db.add_all([project, foreign_project])
            db.flush()
            db.add(
                Task(
                    project_id=project.id,
                    title="Background task",
                    status="todo",
                    priority="medium",
                )
            )
            db.commit()
            self.tenant_id = tenant.id
            self.other_tenant_id = other_tenant.id
            self.project_id = project.id
            self.foreign_project_id = foreign_project.id

        self.redis = FakeRedis()
        self.redis_patch = patch.object(
            cache, "get_redis_client", return_value=self.redis
        )
        self.redis_patch.start()
        self.session_patch = patch.object(
            project_risk_tasks, "SessionLocal", self.session_factory
        )
        self.session_patch.start()
        self._eager_settings = (
            celery_app.conf.task_always_eager,
            celery_app.conf.task_eager_propagates,
        )
        celery_app.conf.update(
            task_always_eager=True,
            task_eager_propagates=False,
        )

    def tearDown(self) -> None:
        celery_app.conf.update(
            task_always_eager=self._eager_settings[0],
            task_eager_propagates=self._eager_settings[1],
        )
        self.session_patch.stop()
        self.redis_patch.stop()
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        del _record
        connection.execute("PRAGMA foreign_keys=ON")

    def _new_version(self) -> str:
        version = str(uuid4())
        self.redis.set(
            project_risk_version_key(self.tenant_id, self.project_id),
            version,
            86400,
        )
        return version

    def test_task_calculates_and_caches_only_for_tenant_project(self) -> None:
        version = self._new_version()
        result = project_risk_tasks.recalculate_project_risk.apply(
            args=(self.tenant_id, self.project_id, version, True),
            throw=False,
        )

        self.assertTrue(result.successful())
        self.assertEqual(result.result["status"], "updated")
        self.assertEqual(result.result["tenant_id"], self.tenant_id)
        self.assertIn(
            project_risk_cache_key(self.tenant_id, self.project_id),
            self.redis.values,
        )

    def test_foreign_tenant_project_is_not_analyzed_or_cached(self) -> None:
        version = str(uuid4())
        with patch.object(
            project_risk_tasks, "analyze_project_risk"
        ) as analyze:
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.foreign_project_id, version, True),
                throw=False,
            )

        self.assertTrue(result.successful())
        self.assertEqual(result.result["status"], "not_found")
        analyze.assert_not_called()
        self.assertEqual(self.redis.get_calls, [])
        self.assertEqual(self.redis.values, {})

    def test_tenant_scope_is_rechecked_after_database_retry(self) -> None:
        calls = 0

        def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("select", {}, RuntimeError("temporary"))
            return self.session_factory()

        with patch.object(
            project_risk_tasks, "SessionLocal", side_effect=fail_once
        ), patch.object(project_risk_tasks, "analyze_project_risk") as analyze:
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.foreign_project_id, str(uuid4()), True),
                throw=False,
            )
        self.assertTrue(result.successful())
        self.assertEqual(result.result["status"], "not_found")
        self.assertEqual(calls, 2)
        analyze.assert_not_called()

    def test_superseded_job_cannot_overwrite_newer_version(self) -> None:
        newer_version = self._new_version()
        with patch.object(
            project_risk_tasks, "analyze_project_risk"
        ) as analyze:
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, "stale-version", True),
                throw=False,
            )

        self.assertTrue(result.successful())
        self.assertEqual(result.result["status"], "superseded")
        analyze.assert_not_called()
        self.assertEqual(
            self.redis.values[
                project_risk_version_key(self.tenant_id, self.project_id)
            ],
            newer_version,
        )

    def test_duplicate_job_is_idempotently_skipped(self) -> None:
        version = self._new_version()
        with patch.object(
            project_risk_tasks,
            "analyze_project_risk",
            return_value={"risk_score": 8},
        ) as analyze:
            first = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )
            duplicate = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )

        self.assertTrue(first.successful())
        self.assertTrue(duplicate.successful())
        self.assertEqual(first.result["status"], "updated")
        self.assertEqual(duplicate.result["status"], "duplicate")
        self.assertEqual(analyze.call_count, 1)

    def test_synchronous_cache_population_completes_following_job(self) -> None:
        version = self._new_version()
        with patch.object(
            project_risk_cache,
            "analyze_project_risk",
            wraps=project_risk_cache.analyze_project_risk,
        ) as analyze:
            with self.session_factory() as db:
                project = db.execute(
                    select(Project).where(Project.id == self.project_id)
                ).scalar_one()
                synchronous_result = project_risk_cache.get_project_risk(db, project)

            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )

        self.assertTrue(result.successful(), repr(result.result))
        self.assertEqual(result.result["status"], "duplicate")
        self.assertEqual(analyze.call_count, 1)
        completion_key = project_risk_job_completion_key(
            self.tenant_id, self.project_id, version
        )
        self.assertEqual(self.redis.values[completion_key], version)
        self.assertEqual(
            json.loads(
                self.redis.values[
                    project_risk_cache_key(self.tenant_id, self.project_id)
                ]
            )["risk_score"],
            synchronous_result["risk_score"],
        )

    def test_completion_marker_lua_requires_current_version_and_cache(self) -> None:
        version = self._new_version()
        result_key = project_risk_cache_key(self.tenant_id, self.project_id)
        version_key = project_risk_version_key(self.tenant_id, self.project_id)
        completion_key = project_risk_job_completion_key(
            self.tenant_id, self.project_id, version
        )
        self.redis.set(result_key, '{"risk_score":1}', 60)

        self.redis.values[version_key] = "newer-version"
        self.assertFalse(
            project_risk_cache.mark_project_risk_job_complete_if_cached(
                self.tenant_id, self.project_id, version
            )
        )
        self.assertNotIn(completion_key, self.redis.values)

        self.redis.values[version_key] = version
        self.redis.values.pop(result_key)
        self.assertFalse(
            project_risk_cache.mark_project_risk_job_complete_if_cached(
                self.tenant_id, self.project_id, version
            )
        )
        self.assertNotIn(completion_key, self.redis.values)

        self.redis.set(result_key, '{"risk_score":1}', 60)
        self.assertTrue(
            project_risk_cache.mark_project_risk_job_complete_if_cached(
                self.tenant_id, self.project_id, version
            )
        )
        self.assertEqual(self.redis.values[completion_key], version)
        script, key_count, args = self.redis.eval_calls[-1]
        self.assertEqual(key_count, 3)
        self.assertEqual(
            args[:3],
            (version_key, result_key, completion_key),
        )
        self.assertIn("redis.call('GET', KEYS[1]) ~= ARGV[1]", script)
        self.assertIn("redis.call('EXISTS', KEYS[2]) == 0", script)
        self.assertIn(
            "redis.call('SET', KEYS[3], ARGV[1], 'EX', ARGV[2])",
            script,
        )

    def test_concurrent_duplicate_lock_defers_without_recalculating(self) -> None:
        version = self._new_version()
        lock_key = (
            f"{project_risk_cache_key(self.tenant_id, self.project_id)}"
            f":job-lock:{version}"
        )
        self.redis.set(lock_key, "other-worker-token", 60)
        with patch.object(project_risk_tasks, "analyze_project_risk") as analyze:
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )
        self.assertTrue(result.successful())
        self.assertEqual(result.result["status"], "deferred")
        analyze.assert_not_called()

    def test_transient_database_failure_retries_and_recovers(self) -> None:
        version = self._new_version()
        calls = 0

        def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("select", {}, RuntimeError("temporary"))
            return self.session_factory()

        with patch.object(project_risk_tasks, "SessionLocal", side_effect=fail_once):
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )

        self.assertTrue(result.successful(), repr(result.result))
        self.assertEqual(calls, 2)

    def test_permanent_failure_is_reported_and_redis_outage_still_calculates(self) -> None:
        version = self._new_version()
        with patch.object(
            project_risk_tasks,
            "analyze_project_risk",
            side_effect=ValueError("invalid test result"),
        ):
            failed = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )
        self.assertTrue(failed.failed())

        self.redis.fail_get = True
        with patch.object(
            project_risk_tasks,
            "analyze_project_risk",
            return_value={"risk_score": 4},
        ) as analyze:
            fallback = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )
        self.assertTrue(fallback.successful())
        self.assertEqual(fallback.result["status"], "calculated")
        analyze.assert_called_once()

    def test_redis_failure_retries_then_recovers(self) -> None:
        version = self._new_version()
        original_get = self.redis.get
        failures_left = 1

        def fail_once(key: str):
            nonlocal failures_left
            if failures_left:
                failures_left -= 1
                raise ConnectionError("temporary Redis outage")
            return original_get(key)

        with patch.object(self.redis, "get", side_effect=fail_once):
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )
        self.assertTrue(result.successful(), repr(result.result))
        self.assertEqual(result.result["status"], "updated")
        self.assertEqual(failures_left, 0)

    def test_transient_redis_cache_write_failure_retries(self) -> None:
        version = self._new_version()
        self.redis.fail_eval_count = 1
        with patch.object(
            project_risk_tasks,
            "analyze_project_risk",
            return_value={"risk_score": 7},
        ) as analyze:
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )
        self.assertTrue(result.successful(), repr(result.result))
        self.assertEqual(result.result["status"], "updated")
        self.assertEqual(analyze.call_count, 2)

    def test_permanent_redis_outage_retries_then_uses_postgres(self) -> None:
        version = self._new_version()
        self.redis.fail_get = True
        with patch.object(
            project_risk_tasks,
            "analyze_project_risk",
            return_value={"risk_score": 5},
        ) as analyze, self.assertLogs(
            "backend.app.tasks.project_risk", level="WARNING"
        ) as logs:
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )
        self.assertTrue(result.successful())
        self.assertEqual(result.result["status"], "calculated")
        analyze.assert_called_once()
        self.assertTrue(
            any("project_risk_job_redis_fallback" in line for line in logs.output)
        )

    def test_permanent_database_failure_exhausts_retries_and_logs_failure(self) -> None:
        version = self._new_version()
        calls = 0

        def always_fail():
            nonlocal calls
            calls += 1
            raise OperationalError("select", {}, RuntimeError("database down"))

        with patch.object(
            project_risk_tasks, "SessionLocal", side_effect=always_fail
        ), self.assertLogs("backend.app.tasks.project_risk", level="ERROR") as logs:
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )
        self.assertTrue(result.failed())
        self.assertEqual(calls, 4)
        self.assertIn("project_risk_job_database_failed", logs.output[-1])

    def test_task_logs_success_with_safe_structured_context(self) -> None:
        version = self._new_version()
        with self.assertLogs("backend.app.tasks.project_risk", level="INFO") as logs:
            result = project_risk_tasks.recalculate_project_risk.apply(
                args=(self.tenant_id, self.project_id, version, True),
                throw=False,
            )
        self.assertTrue(result.successful())
        self.assertIn("project_risk_job_succeeded", logs.output[-1])
        success_record = next(
            record
            for record in logs.records
            if record.event_name == "project_risk_job_succeeded"
        )
        self.assertEqual(success_record.tenant_id, self.tenant_id)
        self.assertEqual(success_record.project_id, self.project_id)

    def test_periodic_scheduler_enqueues_tenant_scoped_jobs(self) -> None:
        with patch.object(
            project_risk_tasks,
            "enqueue_project_risk_recalculation",
            return_value="queued",
        ) as enqueue:
            result = project_risk_tasks.schedule_periodic_project_risk_checks.apply(
                throw=False
            )

        self.assertTrue(result.successful())
        self.assertEqual(result.result, {"queued": 2})
        self.assertEqual(
            enqueue.call_args_list,
            [
                unittest.mock.call(self.tenant_id, self.project_id),
                unittest.mock.call(self.other_tenant_id, self.foreign_project_id),
            ],
        )

    def test_periodic_scheduler_retries_transient_database_failure(self) -> None:
        calls = 0

        def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("select", {}, RuntimeError("temporary"))
            return self.session_factory()

        with patch.object(
            project_risk_tasks, "SessionLocal", side_effect=fail_once
        ), patch.object(
            project_risk_tasks,
            "enqueue_project_risk_recalculation",
            return_value="queued",
        ):
            result = project_risk_tasks.schedule_periodic_project_risk_checks.apply(
                throw=False
            )
        self.assertTrue(result.successful(), repr(result.result))
        self.assertEqual(result.result, {"queued": 2})
        self.assertEqual(calls, 2)

    def test_beat_schedule_runs_periodic_risk_checks_every_15_minutes(self) -> None:
        entry = celery_app.conf.beat_schedule[
            "schedule-project-risk-checks-every-15-minutes"
        ]
        self.assertEqual(
            entry["task"],
            "backend.app.tasks.project_risk.schedule_periodic_checks",
        )
        self.assertEqual(entry["schedule"], crontab(minute="*/15"))
        self.assertEqual(project_risk_tasks.recalculate_project_risk.max_retries, 3)
        self.assertEqual(project_risk_tasks.recalculate_project_risk.soft_time_limit, 30)
        self.assertEqual(project_risk_tasks.recalculate_project_risk.time_limit, 45)

    def test_enqueue_registers_job_version_and_continues_without_redis(self) -> None:
        with patch.object(
            project_risk_tasks.recalculate_project_risk,
            "apply_async",
            return_value=type("Result", (), {"id": "job-id"})(),
        ) as apply_async:
            job_id = enqueue_project_risk_recalculation(
                self.tenant_id, self.project_id
            )
        self.assertEqual(job_id, "job-id")
        version_key = project_risk_version_key(
            self.tenant_id, self.project_id
        )
        registered_version = self.redis.values[version_key]
        self.assertEqual(apply_async.call_args.kwargs["args"][:2], (
            self.tenant_id,
            self.project_id,
        ))
        self.assertEqual(apply_async.call_args.kwargs["args"][2], registered_version)
        self.assertTrue(apply_async.call_args.kwargs["args"][3])

        with (
            patch.object(cache, "set_value", side_effect=ConnectionError("offline")),
            patch.object(
                project_risk_tasks.recalculate_project_risk,
                "apply_async",
                return_value=type("Result", (), {"id": "fallback-job"})(),
            ) as fallback_apply,
        ):
            fallback_id = enqueue_project_risk_recalculation(
                self.tenant_id, self.project_id
            )
        self.assertEqual(fallback_id, "fallback-job")
        self.assertFalse(fallback_apply.call_args.kwargs["args"][3])

    def test_enqueue_failure_is_reported_without_raising(self) -> None:
        with patch.object(
            project_risk_tasks.recalculate_project_risk,
            "apply_async",
            side_effect=KombuOperationalError("offline"),
        ):
            job_id = enqueue_project_risk_recalculation(
                self.tenant_id, self.project_id
            )
        self.assertIsNone(job_id)


if __name__ == "__main__":
    unittest.main()
