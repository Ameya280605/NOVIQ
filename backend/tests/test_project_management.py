import os
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "project-test-signing-key"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import AuditLog, Base, Project, Task, Tenant, User
from backend.app.utils.jwt import create_access_token


class ProjectManagementTests(unittest.TestCase):
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
            tenant = Tenant(name="Project Tenant")
            other_tenant = Tenant(name="Other Tenant")
            db.add_all([tenant, other_tenant])
            db.flush()
            owner = User(
                tenant_id=tenant.id,
                name="Owner",
                email="project-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            member = User(
                tenant_id=tenant.id,
                name="Member",
                email="project-member@example.test",
                password_hash="not-a-real-hash",
                role="member",
            )
            manager = User(
                tenant_id=tenant.id,
                name="Manager",
                email="project-manager@example.test",
                password_hash="not-a-real-hash",
                role="manager",
            )
            foreign_manager = User(
                tenant_id=other_tenant.id,
                name="Foreign Manager",
                email="foreign-manager@example.test",
                password_hash="not-a-real-hash",
                role="manager",
            )
            foreign_owner = User(
                tenant_id=other_tenant.id,
                name="Foreign Owner",
                email="foreign-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            db.add_all([owner, member, manager, foreign_manager, foreign_owner])
            db.commit()
            self.tenant_id = tenant.id
            self.other_tenant_id = other_tenant.id
            self.owner_id = owner.id
            self.member_id = member.id
            self.manager_id = manager.id
            self.foreign_manager_id = foreign_manager.id
            self.foreign_owner_id = foreign_owner.id

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
        self.foreign_headers = self._headers(
            self.foreign_manager_id, self.other_tenant_id, "manager"
        )
        self.foreign_owner_headers = self._headers(
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

    def _create_project(self, **data) -> dict:
        response = self.client.post(
            "/projects/", headers=self.owner_headers, json=data
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_project_crud_lifecycle_and_audit(self) -> None:
        created = self._create_project(
            name="Lifecycle project",
            description="Project description",
            start_date="2026-11-01",
            target_deadline="2026-12-10",
            project_manager_id=self.manager_id,
        )
        self.assertEqual(created["status"], "planned")
        self.assertEqual(created["project_manager_id"], self.manager_id)
        project_id = created["id"]

        viewed = self.client.get(
            f"/projects/{project_id}", headers=self.member_headers
        )
        self.assertEqual(viewed.status_code, 200, viewed.text)
        self.assertEqual(viewed.json()["tenant_id"], self.tenant_id)

        updated = self.client.put(
            f"/projects/{project_id}",
            headers=self.owner_headers,
            json={"name": "Lifecycle project updated", "status": "active"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["status"], "active")

        completion_day = datetime.now(timezone.utc).date().isoformat()
        completed = self.client.put(
            f"/projects/{project_id}",
            headers=self.owner_headers,
            json={"status": "completed"},
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["completion_date"], completion_day)

        cannot_reopen = self.client.put(
            f"/projects/{project_id}",
            headers=self.owner_headers,
            json={"status": "active"},
        )
        self.assertEqual(cannot_reopen.status_code, 409)

        deleted = self.client.delete(
            f"/projects/{project_id}", headers=self.owner_headers
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        missing = self.client.get(
            f"/projects/{project_id}", headers=self.owner_headers
        )
        self.assertEqual(missing.status_code, 404)

        with self.session_factory() as db:
            events = (
                db.query(AuditLog)
                .filter(
                    AuditLog.resource_type == "project",
                    AuditLog.resource_id == project_id,
                )
                .order_by(AuditLog.id)
                .all()
            )
            self.assertEqual(
                [row.action for row in events],
                [
                    "project_created",
                    "project_manager_assigned",
                    "project_updated",
                    "project_status_changed",
                    "project_status_changed",
                    "project_deleted",
                ],
            )
            self.assertTrue(all(row.tenant_id == self.tenant_id for row in events))
            self.assertTrue(all(row.user_id == self.owner_id for row in events))
            self.assertEqual(
                events[1].metadata_json,
                {"new_project_manager_id": self.manager_id},
            )
            self.assertEqual(
                events[3].metadata_json,
                {"old_status": "planned", "new_status": "active"},
            )

    def test_manager_assignment_and_update_auditing(self) -> None:
        project = self._create_project(name="Managed")
        project_id = project["id"]

        assigned = self.client.put(
            f"/projects/{project_id}",
            headers=self.owner_headers,
            json={"project_manager_id": self.manager_id},
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(assigned.json()["project_manager_id"], self.manager_id)

        unassigned = self.client.put(
            f"/projects/{project_id}",
            headers=self.owner_headers,
            json={"project_manager_id": None},
        )
        self.assertEqual(unassigned.status_code, 200, unassigned.text)
        self.assertIsNone(unassigned.json()["project_manager_id"])

        with self.session_factory() as db:
            events = (
                db.query(AuditLog)
                .filter(
                    AuditLog.resource_id == project_id,
                    AuditLog.action == "project_manager_assigned",
                )
                .order_by(AuditLog.id)
                .all()
            )
            self.assertEqual(len(events), 2)
            self.assertEqual(
                events[0].metadata_json,
                {"old_project_manager_id": None,
                 "new_project_manager_id": self.manager_id},
            )
            self.assertEqual(
                events[1].metadata_json,
                {"old_project_manager_id": self.manager_id,
                 "new_project_manager_id": None},
            )

    def test_project_filters_pagination_and_tenant_isolation(self) -> None:
        self._create_project(name="Alpha One", status="planned")
        self._create_project(name="Alpha Two", status="active")
        self._create_project(
            name="Beta", status="active", project_manager_id=self.manager_id
        )
        foreign_project = self.client.post(
            "/projects/",
            headers=self.foreign_headers,
            json={"name": "Foreign project"},
        )
        self.assertEqual(foreign_project.status_code, 200, foreign_project.text)

        response = self.client.get(
            "/projects/",
            headers=self.owner_headers,
            params={"search": "alpha", "page": 2, "page_size": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 2)
        self.assertEqual(response.json()["total_pages"], 2)
        self.assertEqual(len(response.json()["items"]), 1)

        filtered = self.client.get(
            "/projects/",
            headers=self.owner_headers,
            params={"status": "active", "manager_id": self.manager_id},
        )
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["name"], "Beta")

        tenant_list = self.client.get(
            "/projects/", headers=self.foreign_headers
        )
        self.assertEqual(tenant_list.status_code, 200, tenant_list.text)
        self.assertEqual(tenant_list.json()["total"], 1)
        self.assertEqual(
            tenant_list.json()["items"][0]["id"], foreign_project.json()["id"]
        )
        hidden = self.client.get(
            f"/projects/{foreign_project.json()['id']}",
            headers=self.owner_headers,
        )
        self.assertEqual(hidden.status_code, 404)

    def test_invalid_dates_status_and_manager_are_rejected(self) -> None:
        bad_status = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={"name": "Bad", "status": "unknown"},
        )
        self.assertEqual(bad_status.status_code, 422)

        bad_dates = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={
                "name": "Bad dates",
                "start_date": "2026-12-10",
                "target_deadline": "2026-11-10",
            },
        )
        self.assertEqual(bad_dates.status_code, 422)

        early_completion = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={
                "name": "Bad completion",
                "status": "completed",
                "start_date": "2026-11-10",
                "completion_date": "2026-11-09",
            },
        )
        self.assertEqual(early_completion.status_code, 422)

        incomplete_completion = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={
                "name": "Premature completion date",
                "completion_date": "2026-11-10",
            },
        )
        self.assertEqual(incomplete_completion.status_code, 422)

        wrong_tenant_manager = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={
                "name": "Foreign manager",
                "project_manager_id": self.foreign_manager_id,
            },
        )
        self.assertEqual(wrong_tenant_manager.status_code, 404)

        non_manager = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={"name": "Non-manager", "project_manager_id": self.member_id},
        )
        self.assertEqual(non_manager.status_code, 404)
        with self.session_factory() as db:
            self.assertEqual(db.query(AuditLog).count(), 0)

    def test_authorization_and_cross_tenant_mutations(self) -> None:
        project = self._create_project(name="Private project")
        project_id = project["id"]

        unauthenticated = self.client.get("/projects/")
        self.assertIn(unauthenticated.status_code, (401, 403))

        unauthorized_create = self.client.post(
            "/projects/",
            headers=self.member_headers,
            json={"name": "Member project"},
        )
        self.assertEqual(unauthorized_create.status_code, 403)

        unauthorized_delete = self.client.delete(
            f"/projects/{project_id}", headers=self.member_headers
        )
        self.assertEqual(unauthorized_delete.status_code, 403)

        wrong_tenant_read = self.client.get(
            f"/projects/{project_id}", headers=self.foreign_headers
        )
        self.assertEqual(wrong_tenant_read.status_code, 404)
        wrong_tenant_update = self.client.put(
            f"/projects/{project_id}",
            headers=self.foreign_headers,
            json={"name": "Tampered"},
        )
        self.assertEqual(wrong_tenant_update.status_code, 404)
        wrong_tenant_delete = self.client.delete(
            f"/projects/{project_id}", headers=self.foreign_owner_headers
        )
        self.assertEqual(wrong_tenant_delete.status_code, 404)

    def test_delete_is_rejected_when_project_has_tasks(self) -> None:
        project = self._create_project(name="Has tasks")
        with self.session_factory() as db:
            db.add(Task(project_id=project["id"], title="Retained task"))
            db.commit()

        response = self.client.delete(
            f"/projects/{project['id']}", headers=self.owner_headers
        )
        self.assertEqual(response.status_code, 409)
        with self.session_factory() as db:
            self.assertIsNotNone(db.query(Project).filter_by(id=project["id"]).first())
            self.assertIsNone(
                db.query(AuditLog)
                .filter_by(
                    action="project_deleted",
                    resource_id=project["id"],
                )
                .first()
            )


if __name__ == "__main__":
    unittest.main()
