import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "dashboard-test-signing-key"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import Base, Project, Task, Tenant, User
from backend.app.utils.jwt import create_access_token


class DashboardAnalyticsTests(unittest.TestCase):
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
        today = now.date()
        with self.session_factory() as db:
            tenant = Tenant(name="Dashboard Tenant")
            other_tenant = Tenant(name="Other Dashboard Tenant")
            db.add_all([tenant, other_tenant])
            db.flush()
            owner = User(
                tenant_id=tenant.id,
                name="Dashboard Owner",
                email="dashboard-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            member = User(
                tenant_id=tenant.id,
                name="Dashboard Member",
                email="dashboard-member@example.test",
                password_hash="not-a-real-hash",
                role="member",
            )
            second_member = User(
                tenant_id=tenant.id,
                name="Second Dashboard Member",
                email="dashboard-member2@example.test",
                password_hash="not-a-real-hash",
                role="member",
            )
            other_owner = User(
                tenant_id=other_tenant.id,
                name="Other Owner",
                email="dashboard-other-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            db.add_all([owner, member, second_member, other_owner])
            db.flush()
            active_project = Project(
                tenant_id=tenant.id,
                name="Active project",
                status="active",
                target_deadline=today + timedelta(days=5),
                created_at=now - timedelta(days=2),
            )
            completed_project = Project(
                tenant_id=tenant.id,
                name="Completed project",
                status="completed",
                completion_date=today,
                created_at=now - timedelta(days=1),
            )
            other_project = Project(
                tenant_id=other_tenant.id,
                name="Other project",
                status="active",
                target_deadline=today + timedelta(days=2),
            )
            db.add_all([active_project, completed_project, other_project])
            db.flush()
            overdue_task = Task(
                project_id=active_project.id,
                title="Overdue alpha",
                status="todo",
                priority="high",
                due_date=now - timedelta(days=1),
                assigned_to=member.id,
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=2),
            )
            completed_task = Task(
                project_id=completed_project.id,
                title="Completed beta",
                status="completed",
                priority="medium",
                due_date=now - timedelta(days=2),
                assigned_to=member.id,
                created_at=now - timedelta(days=1),
                updated_at=now,
            )
            upcoming_task = Task(
                project_id=active_project.id,
                title="Upcoming gamma",
                status="in_progress",
                priority="critical",
                due_date=now + timedelta(days=2),
                assigned_to=second_member.id,
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=2),
            )
            foreign_task = Task(
                project_id=other_project.id,
                title="Foreign task",
                status="todo",
                priority="critical",
                due_date=now + timedelta(days=1),
                assigned_to=other_owner.id,
                created_at=now,
                updated_at=now,
            )
            db.add_all([overdue_task, completed_task, upcoming_task, foreign_task])
            db.commit()
            self.tenant_id = tenant.id
            self.other_tenant_id = other_tenant.id
            self.owner_id = owner.id
            self.member_id = member.id
            self.other_owner_id = other_owner.id
            self.active_project_id = active_project.id
            self.completed_project_id = completed_project.id

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

    def tearDown(self) -> None:
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

    def test_overview_counts_percentages_and_tenant_scope(self) -> None:
        response = self.client.get(
            "/dashboard/overview",
            headers=self.owner_headers,
            params={"tenant_id": self.other_tenant_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(
            result["projects"],
            {
                "total": 2,
                "active": 1,
                "completed": 1,
                "completion_percentage": 50.0,
            },
        )
        self.assertEqual(
            result["tasks"],
            {
                "total": 3,
                "completed": 1,
                "pending": 2,
                "overdue": 1,
                "completion_percentage": 33.33,
            },
        )

    def test_task_analytics_distribution_deadlines_trend_and_date_filters(self):
        response = self.client.get(
            "/dashboard/tasks", headers=self.owner_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(
            {item["name"]: item["count"] for item in result["status_distribution"]},
            {"completed": 1, "in_progress": 1, "todo": 1},
        )
        self.assertEqual(
            {item["name"]: item["count"] for item in result["priority_distribution"]},
            {"critical": 1, "high": 1, "medium": 1},
        )
        self.assertEqual(
            [item["title"] for item in result["overdue_tasks"]],
            ["Overdue alpha"],
        )
        self.assertEqual(
            [item["title"] for item in result["upcoming_deadlines"]],
            ["Upcoming gamma"],
        )
        today = datetime.now(timezone.utc).date().isoformat()
        trend_by_day = {item["date"]: item["count"] for item in result["completion_trend"]}
        self.assertEqual(trend_by_day[today], 1)
        self.assertEqual(len(result["completion_trend"]), 30)

        today_only = self.client.get(
            "/dashboard/tasks",
            headers=self.owner_headers,
            params={"start_date": today, "end_date": today},
        )
        self.assertEqual(today_only.status_code, 200, today_only.text)
        self.assertEqual(
            sum(
                item["count"]
                for item in today_only.json()["status_distribution"]
            ),
            0,
        )
        self.assertEqual(
            today_only.json()["start_date"],
            today,
        )

    def test_project_analytics_progress_deadlines_and_tenant_scope(self):
        response = self.client.get(
            "/dashboard/projects", headers=self.owner_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(
            {item["name"]: item["count"] for item in result["status_distribution"]},
            {"active": 1, "completed": 1},
        )
        active = next(
            item
            for item in result["progress_by_project"]
            if item["project_id"] == self.active_project_id
        )
        completed = next(
            item
            for item in result["progress_by_project"]
            if item["project_id"] == self.completed_project_id
        )
        self.assertEqual(
            (active["total_tasks"], active["completed_tasks"],
             active["completion_percentage"]),
            (2, 0, 0.0),
        )
        self.assertEqual(
            (completed["total_tasks"], completed["completed_tasks"],
             completed["completion_percentage"]),
            (1, 1, 100.0),
        )
        self.assertEqual(
            [item["name"] for item in result["upcoming_deadlines"]],
            ["Active project"],
        )

    def test_team_workload_includes_zero_task_members_and_scopes_tenant(self):
        response = self.client.get("/dashboard/team", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200, response.text)
        members = {member["name"]: member for member in response.json()["members"]}
        self.assertNotIn("Other Owner", members)
        self.assertEqual(
            (members["Dashboard Member"]["assigned_tasks"],
             members["Dashboard Member"]["completed_tasks"],
             members["Dashboard Member"]["pending_tasks"],
             members["Dashboard Member"]["workload_percentage"]),
            (2, 1, 1, 66.67),
        )
        self.assertEqual(
            (members["Second Dashboard Member"]["assigned_tasks"],
             members["Second Dashboard Member"]["pending_tasks"],
             members["Second Dashboard Member"]["workload_percentage"]),
            (1, 1, 33.33),
        )
        self.assertEqual(members["Dashboard Owner"]["assigned_tasks"], 0)

    def test_empty_tenant_returns_zeroes_and_empty_analytics(self):
        with self.session_factory() as db:
            empty_tenant = Tenant(name="Empty tenant")
            db.add(empty_tenant)
            db.flush()
            empty_owner = User(
                tenant_id=empty_tenant.id,
                name="Empty Owner",
                email="dashboard-empty-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            db.add(empty_owner)
            db.commit()
            headers = self._headers(empty_owner.id, empty_tenant.id, "owner")

        overview = self.client.get("/dashboard/overview", headers=headers)
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["projects"]["total"], 0)
        self.assertEqual(overview.json()["projects"]["completion_percentage"], 0.0)
        self.assertEqual(overview.json()["tasks"]["overdue"], 0)

        for endpoint in ("/dashboard/tasks", "/dashboard/projects", "/dashboard/team"):
            result = self.client.get(endpoint, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            if endpoint.endswith("/tasks"):
                self.assertEqual(result.json()["status_distribution"], [])
                self.assertEqual(result.json()["overdue_tasks"], [])
            elif endpoint.endswith("/projects"):
                self.assertEqual(result.json()["progress_by_project"], [])
            else:
                self.assertEqual(len(result.json()["members"]), 1)
                self.assertEqual(result.json()["members"][0]["assigned_tasks"], 0)

    def test_authentication_and_date_range_validation(self):
        unauthenticated = self.client.get("/dashboard/overview")
        self.assertIn(unauthenticated.status_code, (401, 403))

        forbidden = self.client.get(
            "/dashboard/tasks",
            headers=self.member_headers,
            params={"start_date": "2026-12-01", "end_date": "2026-11-01"},
        )
        self.assertEqual(forbidden.status_code, 422)

        too_wide = self.client.get(
            "/dashboard/team",
            headers=self.member_headers,
            params={"start_date": "2024-01-01", "end_date": "2026-01-01"},
        )
        self.assertEqual(too_wide.status_code, 422)

        allowed = self.client.get(
            "/dashboard/overview",
            headers=self.member_headers,
            params={"start_date": "2026-01-01", "end_date": "2026-01-02"},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["projects"]["total"], 0)

        with self.session_factory() as db:
            viewer = User(
                tenant_id=self.tenant_id,
                name="Viewer",
                email="dashboard-viewer@example.test",
                password_hash="not-a-real-hash",
                role="viewer",
            )
            db.add(viewer)
            db.commit()
            viewer_headers = self._headers(viewer.id, self.tenant_id, "owner")
        stale_privileged_role = self.client.get(
            "/dashboard/overview", headers=viewer_headers
        )
        self.assertEqual(stale_privileged_role.status_code, 403)

if __name__ == "__main__":
    unittest.main()
