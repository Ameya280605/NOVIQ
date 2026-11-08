import os
import unittest

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "task-test-signing-key"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import AuditLog, Base, Project, Task, Tenant, User
from backend.app.utils.jwt import create_access_token


class TaskManagementTests(unittest.TestCase):
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
            tenant = Tenant(name="Task Tenant")
            other_tenant = Tenant(name="Other Task Tenant")
            db.add_all([tenant, other_tenant])
            db.flush()
            owner = User(
                tenant_id=tenant.id,
                name="Owner",
                email="task-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            member = User(
                tenant_id=tenant.id,
                name="Member",
                email="task-member@example.test",
                password_hash="not-a-real-hash",
                role="member",
            )
            second_member = User(
                tenant_id=tenant.id,
                name="Second Member",
                email="task-member2@example.test",
                password_hash="not-a-real-hash",
                role="member",
            )
            manager = User(
                tenant_id=tenant.id,
                name="Manager",
                email="task-manager@example.test",
                password_hash="not-a-real-hash",
                role="manager",
            )
            foreign_owner = User(
                tenant_id=other_tenant.id,
                name="Foreign Owner",
                email="task-foreign-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            db.add_all([owner, member, second_member, manager, foreign_owner])
            db.flush()
            project = Project(tenant_id=tenant.id, name="Tenant Project")
            foreign_project = Project(
                tenant_id=other_tenant.id, name="Foreign Project"
            )
            db.add_all([project, foreign_project])
            db.commit()
            self.tenant_id = tenant.id
            self.other_tenant_id = other_tenant.id
            self.owner_id = owner.id
            self.member_id = member.id
            self.second_member_id = second_member.id
            self.manager_id = manager.id
            self.foreign_owner_id = foreign_owner.id
            self.project_id = project.id
            self.foreign_project_id = foreign_project.id

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.owner_headers = self._headers(
            self.owner_id, self.tenant_id, "owner"
        )
        self.member_headers = self._headers(
            self.member_id, self.tenant_id, "member"
        )
        self.manager_headers = self._headers(
            self.manager_id, self.tenant_id, "manager"
        )
        self.foreign_headers = self._headers(
            self.foreign_owner_id, self.other_tenant_id, "owner"
        )

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

    def _create_task(self, **data) -> dict:
        response = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={"project_id": self.project_id, "title": "Task", **data},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_create_read_update_status_and_delete_with_audit(self) -> None:
        task = self._create_task(
            description="A task description",
            start_date="2026-11-01",
            due_date="2026-11-30T12:00:00Z",
        )
        self.assertEqual(task["status"], "todo")
        self.assertEqual(task["priority"], "medium")
        self.assertEqual(task["project_id"], self.project_id)
        task_id = task["id"]

        read = self.client.get(
            f"/tasks/{task_id}", headers=self.member_headers
        )
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["id"], task_id)

        update = self.client.put(
            f"/tasks/{task_id}",
            headers=self.manager_headers,
            json={
                "title": "Updated task",
                "status": "in_progress",
                "priority": "high",
                "start_date": "2026-11-02",
                "due_date": "2026-12-01T12:00:00Z",
            },
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(update.json()["status"], "in_progress")
        self.assertEqual(update.json()["priority"], "high")

        review = self.client.put(
            f"/tasks/{task_id}",
            headers=self.owner_headers,
            json={"status": "in_review"},
        )
        self.assertEqual(review.status_code, 200, review.text)
        completed = self.client.put(
            f"/tasks/{task_id}",
            headers=self.owner_headers,
            json={"status": "completed"},
        )
        self.assertEqual(completed.status_code, 200, completed.text)

        cannot_reopen = self.client.put(
            f"/tasks/{task_id}",
            headers=self.owner_headers,
            json={"status": "todo"},
        )
        self.assertEqual(cannot_reopen.status_code, 409)

        deleted = self.client.delete(
            f"/tasks/{task_id}", headers=self.owner_headers
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(
            self.client.get(
                f"/tasks/{task_id}", headers=self.owner_headers
            ).status_code,
            404,
        )

        with self.session_factory() as db:
            events = (
                db.query(AuditLog)
                .filter(
                    AuditLog.resource_type == "task",
                    AuditLog.resource_id == task_id,
                )
                .order_by(AuditLog.id)
                .all()
            )
            self.assertEqual(
                [row.action for row in events],
                [
                    "task_created",
                    "task_updated",
                    "task_status_changed",
                    "task_status_changed",
                    "task_status_changed",
                    "task_deleted",
                ],
            )
            self.assertTrue(all(row.tenant_id == self.tenant_id for row in events))
            self.assertTrue(
                all(row.user_id in {self.owner_id, self.manager_id} for row in events)
            )
            self.assertEqual(
                events[2].metadata_json,
                {"old_status": "todo", "new_status": "in_progress"},
            )

    def test_assignment_reassignment_unassignment_and_noop_deduplication(self) -> None:
        task = self._create_task(assigned_to=self.member_id)
        task_id = task["id"]
        create_reassign = self.client.put(
            f"/tasks/{task_id}",
            headers=self.owner_headers,
            json={"assigned_to": self.second_member_id},
        )
        self.assertEqual(create_reassign.status_code, 200, create_reassign.text)
        same_assignment = self.client.put(
            f"/tasks/{task_id}",
            headers=self.owner_headers,
            json={"assigned_to": self.second_member_id},
        )
        self.assertEqual(same_assignment.status_code, 200)
        unassign = self.client.put(
            f"/tasks/{task_id}",
            headers=self.owner_headers,
            json={"assigned_to": None},
        )
        self.assertEqual(unassign.status_code, 200, unassign.text)
        self.assertIsNone(unassign.json()["assigned_to"])

        with self.session_factory() as db:
            events = (
                db.query(AuditLog)
                .filter(
                    AuditLog.resource_id == task_id,
                    AuditLog.action == "task_assigned",
                )
                .order_by(AuditLog.id)
                .all()
            )
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0].metadata_json, {"assigned_to": self.member_id})
            self.assertEqual(
                events[1].metadata_json,
                {
                    "old_assigned_to": self.member_id,
                    "new_assigned_to": self.second_member_id,
                },
            )
            self.assertEqual(
                events[2].metadata_json,
                {
                    "old_assigned_to": self.second_member_id,
                    "new_assigned_to": None,
                },
            )

    def test_listing_filters_search_pagination_and_sorting(self) -> None:
        first = self._create_task(title="Alpha plan", status="todo")
        alpha_critical = self._create_task(
            title="Alpha critical",
            status="in_progress",
            priority="critical",
            assigned_to=self.member_id,
        )
        self._create_task(
            title="Beta task",
            status="in_progress",
            priority="high",
            assigned_to=self.second_member_id,
        )

        response = self.client.get(
            "/tasks/",
            headers=self.member_headers,
            params={
                "search": "alpha",
                "status": "in_progress",
                "priority": "critical",
                "assigned_to": self.member_id,
                "project_id": self.project_id,
                "page": 1,
                "limit": 1,
                "sort_by": "title",
                "sort_order": "asc",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"][0]["title"], "Alpha critical")

        paginated = self.client.get(
            "/tasks/",
            headers=self.owner_headers,
            params={"search": "a", "page": 1, "limit": 1},
        )
        self.assertEqual(paginated.status_code, 200, paginated.text)
        self.assertEqual(paginated.json()["total"], 3)
        self.assertEqual(paginated.json()["total_pages"], 3)
        self.assertEqual(len(paginated.json()["items"]), 1)

        second_page = self.client.get(
            "/tasks/",
            headers=self.owner_headers,
            params={"search": "alpha", "page": 2, "limit": 1},
        )
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.json()["total"], 2)
        self.assertEqual(second_page.json()["items"][0]["id"], first["id"])
        self.assertNotEqual(
            second_page.json()["items"][0]["id"], alpha_critical["id"]
        )

    def test_project_move_and_assignee_must_remain_in_tenant(self) -> None:
        task = self._create_task()
        invalid_project = self.client.put(
            f"/tasks/{task['id']}",
            headers=self.owner_headers,
            json={"project_id": self.foreign_project_id},
        )
        self.assertEqual(invalid_project.status_code, 404)
        invalid_assignee = self.client.put(
            f"/tasks/{task['id']}",
            headers=self.owner_headers,
            json={"assigned_to": self.foreign_owner_id},
        )
        self.assertEqual(invalid_assignee.status_code, 404)

        with self.session_factory() as db:
            second_project = Project(
                tenant_id=self.tenant_id, name="Second tenant project"
            )
            db.add(second_project)
            db.commit()
            second_project_id = second_project.id
        moved = self.client.put(
            f"/tasks/{task['id']}",
            headers=self.owner_headers,
            json={"project_id": second_project_id},
        )
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["project_id"], second_project_id)

    def test_invalid_values_dates_references_and_status_transitions(self) -> None:
        invalid_status = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={
                "project_id": self.project_id,
                "title": "Invalid status",
                "status": "finished",
            },
        )
        self.assertEqual(invalid_status.status_code, 422)

        invalid_priority = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={
                "project_id": self.project_id,
                "title": "Invalid priority",
                "priority": "urgent",
            },
        )
        self.assertEqual(invalid_priority.status_code, 422)

        invalid_project = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={"project_id": 999999, "title": "Invalid project"},
        )
        self.assertEqual(invalid_project.status_code, 404)

        invalid_assignment = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={
                "project_id": self.project_id,
                "title": "Foreign assignee",
                "assigned_to": self.foreign_owner_id,
            },
        )
        self.assertEqual(invalid_assignment.status_code, 404)

        invalid_dates = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={
                "project_id": self.project_id,
                "title": "Date order",
                "start_date": "2026-12-02",
                "due_date": "2026-12-01T00:00:00Z",
            },
        )
        self.assertEqual(invalid_dates.status_code, 422)

        task = self._create_task()
        invalid_date_update = self.client.put(
            f"/tasks/{task['id']}",
            headers=self.owner_headers,
            json={
                "start_date": "2026-12-02",
                "due_date": "2026-12-01T00:00:00Z",
            },
        )
        self.assertEqual(invalid_date_update.status_code, 422)
        invalid_transition = self.client.put(
            f"/tasks/{task['id']}",
            headers=self.owner_headers,
            json={"status": "completed"},
        )
        self.assertEqual(invalid_transition.status_code, 409)
        with self.session_factory() as db:
            self.assertEqual(db.query(AuditLog).count(), 1)
            self.assertEqual(
                db.query(Task).filter_by(id=task["id"]).one().status, "todo"
            )

    def test_authorization_and_cross_tenant_read_update_delete(self) -> None:
        task = self._create_task()
        task_id = task["id"]
        unauthenticated = self.client.get("/tasks/")
        self.assertIn(unauthenticated.status_code, (401, 403))

        denied_create = self.client.post(
            "/tasks/",
            headers=self.member_headers,
            json={"project_id": self.project_id, "title": "No create"},
        )
        self.assertEqual(denied_create.status_code, 403)
        denied_update = self.client.put(
            f"/tasks/{task_id}",
            headers=self.member_headers,
            json={"title": "No update"},
        )
        self.assertEqual(denied_update.status_code, 403)
        denied_delete = self.client.delete(
            f"/tasks/{task_id}", headers=self.member_headers
        )
        self.assertEqual(denied_delete.status_code, 403)

        foreign_task = self.client.post(
            "/tasks/",
            headers=self.foreign_headers,
            json={"project_id": self.foreign_project_id, "title": "Foreign task"},
        )
        self.assertEqual(foreign_task.status_code, 200, foreign_task.text)
        foreign_task_id = foreign_task.json()["id"]
        hidden = self.client.get(
            f"/tasks/{foreign_task_id}", headers=self.owner_headers
        )
        self.assertEqual(hidden.status_code, 404)
        hidden_update = self.client.put(
            f"/tasks/{foreign_task_id}",
            headers=self.owner_headers,
            json={"title": "Tampered"},
        )
        self.assertEqual(hidden_update.status_code, 404)
        hidden_delete = self.client.delete(
            f"/tasks/{foreign_task_id}", headers=self.owner_headers
        )
        self.assertEqual(hidden_delete.status_code, 404)

    def test_failed_audit_insert_rolls_back_task_creation(self) -> None:
        def fail_audit_flush(_session, _flush_context, instances):
            del _session, _flush_context
            if any(isinstance(instance, AuditLog) for instance in instances):
                raise RuntimeError("forced audit insert failure")

        event.listen(Session, "before_flush", fail_audit_flush)
        try:
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/tasks/",
                headers=self.owner_headers,
                json={"project_id": self.project_id, "title": "Must roll back"},
            )
            self.assertEqual(response.status_code, 500)
        finally:
            event.remove(Session, "before_flush", fail_audit_flush)

        with self.session_factory() as db:
            self.assertIsNone(
                db.query(Task).filter_by(title="Must roll back").first()
            )
            self.assertIsNone(db.query(AuditLog).first())


if __name__ == "__main__":
    unittest.main()
