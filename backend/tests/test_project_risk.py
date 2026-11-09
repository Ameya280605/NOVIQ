import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "project-risk-test-signing-key"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import AuditLog, Base, Project, Task, Tenant, User
from backend.app.services.project_risk import (
    SCORING_VERSION,
    analyze_project_risk,
)
from backend.app.utils.jwt import create_access_token


class ProjectRiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.session_factory() as db:
            tenant = Tenant(name="Risk Tenant")
            other_tenant = Tenant(name="Other Risk Tenant")
            db.add_all([tenant, other_tenant])
            db.flush()
            owner = User(
                tenant_id=tenant.id,
                name="Risk Owner",
                email="risk-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            member = User(
                tenant_id=tenant.id,
                name="Risk Member",
                email="risk-member@example.test",
                password_hash="not-a-real-hash",
                role="member",
            )
            foreign_owner = User(
                tenant_id=other_tenant.id,
                name="Foreign Risk Owner",
                email="risk-foreign-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            viewer = User(
                tenant_id=tenant.id,
                name="Risk Viewer",
                email="risk-viewer@example.test",
                password_hash="not-a-real-hash",
                role="viewer",
            )
            db.add_all([owner, member, foreign_owner, viewer])
            db.flush()

            self.projects = {}
            for name in ("healthy", "watch", "at_risk", "high_risk", "sparse", "foreign"):
                project_tenant = other_tenant if name == "foreign" else tenant
                project = Project(
                    tenant_id=project_tenant.id,
                    name=f"{name} project",
                    status="active",
                    updated_at=(
                        self.now - timedelta(days=15)
                        if name in {"watch", "at_risk"}
                        else self.now
                    ),
                )
                db.add(project)
                db.flush()
                self.projects[name] = project.id

            def add_task(
                project_name: str,
                title: str,
                *,
                status: str = "todo",
                due_date: datetime | None = None,
                updated_at: datetime | None = None,
                assignee_id: int | None = None,
            ) -> Task:
                task = Task(
                    project_id=self.projects[project_name],
                    title=title,
                    status=status,
                    priority="medium",
                    due_date=due_date,
                    updated_at=updated_at or self.now,
                    assigned_to=assignee_id,
                )
                db.add(task)
                db.flush()
                return task

            add_task("healthy", "Healthy open task")
            add_task(
                "healthy",
                "Invalid legacy foreign assignment",
                assignee_id=foreign_owner.id,
            )
            add_task(
                "watch",
                "Watch due soon one",
                due_date=self.now + timedelta(days=2),
            )
            add_task(
                "watch",
                "Watch due soon two",
                due_date=self.now + timedelta(days=5),
            )

            for index in range(3):
                add_task(
                    "at_risk",
                    f"Overdue task {index}",
                    due_date=self.now - timedelta(days=1),
                )
            repeated_blocker = add_task(
                "at_risk", "Repeated blocker", status="blocked"
            )

            for index in range(5):
                add_task(
                    "high_risk",
                    f"High overdue {index}",
                    due_date=self.now - timedelta(days=2),
                    assignee_id=member.id,
                )
            for index in range(4):
                add_task(
                    "high_risk",
                    f"High due soon {index}",
                    due_date=self.now + timedelta(days=2),
                    assignee_id=member.id,
                )
            blocked_tasks = []
            for index in range(4):
                blocked_tasks.append(
                    add_task(
                        "high_risk",
                        f"High blocked {index}",
                        status="blocked",
                        assignee_id=member.id,
                    )
                )
            for index in range(4):
                add_task(
                    "high_risk",
                    f"High stale {index}",
                    updated_at=self.now - timedelta(days=15),
                    assignee_id=member.id,
                )
            add_task("sparse", "No deadline task")

            for task in (repeated_blocker, blocked_tasks[0]):
                for _ in range(2):
                    db.add(
                        AuditLog(
                            tenant_id=tenant.id,
                            user_id=owner.id,
                            action="task_status_changed",
                            resource_type="task",
                            resource_id=task.id,
                            metadata_json={
                                "old_status": "in_progress",
                                "new_status": "blocked",
                            },
                        )
                    )
            db.commit()
            self.tenant_id = tenant.id
            self.other_tenant_id = other_tenant.id
            self.owner_id = owner.id
            self.member_id = member.id
            self.foreign_owner_id = foreign_owner.id
            self.viewer_id = viewer.id

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.owner_headers = self._headers(self.owner_id, self.tenant_id, "owner")
        self.member_headers = self._headers(self.member_id, self.tenant_id, "member")
        self.foreign_headers = self._headers(
            self.foreign_owner_id, self.other_tenant_id, "owner"
        )
        self.viewer_headers = self._headers(self.viewer_id, self.tenant_id, "owner")
        self.cache_patches = [
            patch("backend.app.services.project_risk_cache.cache.get_value", return_value=None),
            patch("backend.app.services.project_risk_cache.cache.get_json", return_value=None),
            patch(
                "backend.app.services.project_risk_cache.cache.set_json_if_version",
                return_value=True,
            ),
        ]
        for cache_patch in self.cache_patches:
            cache_patch.start()

    def tearDown(self) -> None:
        for cache_patch in self.cache_patches:
            cache_patch.stop()
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

    def _risk(self, project_name: str, headers=None):
        return self.client.get(
            f"/projects/{self.projects[project_name]}/risk-analysis",
            headers=headers or self.owner_headers,
        )

    def test_scoring_labels_explanations_and_missing_dates(self):
        healthy = self._risk("healthy")
        watch = self._risk("watch")
        at_risk = self._risk("at_risk")
        sparse = self._risk("sparse")

        self.assertEqual(healthy.status_code, 200, healthy.text)
        self.assertEqual(
            (healthy.json()["risk_score"], healthy.json()["risk_label"]),
            (0, "healthy"),
        )
        self.assertEqual(healthy.json()["reasons"][0]["code"], "no_signals")
        self.assertEqual(healthy.json()["metrics"]["assigned_open_tasks"], 0)
        self.assertEqual(
            (watch.json()["risk_score"], watch.json()["risk_label"]),
            (20, "watch"),
        )
        self.assertEqual(
            {reason["code"] for reason in watch.json()["reasons"]},
            {"due_soon_tasks", "stale_project"},
        )
        self.assertEqual(
            (at_risk.json()["risk_score"], at_risk.json()["risk_label"]),
            (52, "at_risk"),
        )
        self.assertEqual(
            (sparse.json()["risk_score"], sparse.json()["risk_label"]),
            (0, "healthy"),
        )
        self.assertEqual(sparse.json()["metrics"]["due_soon_tasks"], 0)
        self.assertEqual(sparse.json()["metrics"]["overdue_tasks"], 0)

    def test_high_risk_is_capped_and_bottlenecks_are_actionable(self):
        response = self._risk("high_risk")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["risk_score"], 100)
        self.assertEqual(result["risk_label"], "at_risk")
        self.assertEqual(result["scoring_version"], SCORING_VERSION)
        generated_at = datetime.fromisoformat(
            result["generated_at"].replace("Z", "+00:00")
        )
        self.assertEqual(generated_at.utcoffset(), timedelta(0))

        bottlenecks = result["bottlenecks"]
        self.assertEqual(
            {item["type"] for item in bottlenecks},
            {"repeated_blocked_task", "concentrated_workload"},
        )
        self.assertTrue(all(item["recommendation"] for item in bottlenecks))
        repeated = next(
            item for item in bottlenecks if item["type"] == "repeated_blocked_task"
        )
        self.assertEqual(repeated["count"], 2)
        concentration = next(
            item for item in bottlenecks if item["type"] == "concentrated_workload"
        )
        self.assertEqual(concentration["count"], 17)

    def test_analysis_is_deterministic_for_same_snapshot_and_time(self):
        with self.session_factory() as db:
            project = db.query(Project).filter(
                Project.id == self.projects["high_risk"]
            ).one()
            first = analyze_project_risk(db, project, now=self.now)
            second = analyze_project_risk(db, project, now=self.now)
        self.assertEqual(first, second)

    def test_authentication_database_role_and_tenant_isolation(self):
        unauthenticated = self.client.get(
            f"/projects/{self.projects['healthy']}/risk-analysis"
        )
        self.assertIn(unauthenticated.status_code, (401, 403))

        stale_role = self._risk("healthy", self.viewer_headers)
        self.assertEqual(stale_role.status_code, 403)

        tenant_member_access = self._risk("healthy", self.member_headers)
        self.assertEqual(tenant_member_access.status_code, 200)

        cross_tenant = self._risk("foreign", self.owner_headers)
        self.assertEqual(cross_tenant.status_code, 404)

        foreign_owner_access = self._risk("healthy", self.foreign_headers)
        self.assertEqual(foreign_owner_access.status_code, 404)


if __name__ == "__main__":
    unittest.main()
