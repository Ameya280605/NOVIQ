import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "audit-test-signing-key"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import AuditLog, Base, Project, Tenant, User
from backend.app.utils.jwt import create_access_token
from backend.app.utils.audit import create_audit_log


class AuditLogApiTests(unittest.TestCase):
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
            tenant = Tenant(name="Tenant One")
            other_tenant = Tenant(name="Tenant Two")
            db.add_all([tenant, other_tenant])
            db.flush()
            self.owner = User(
                tenant_id=tenant.id,
                name="Owner",
                email="owner@example.test",
                password_hash="not-a-real-password-hash",
                role="owner",
            )
            self.member = User(
                tenant_id=tenant.id,
                name="Member",
                email="member@example.test",
                password_hash="not-a-real-password-hash",
                role="member",
            )
            self.other_owner = User(
                tenant_id=other_tenant.id,
                name="Other Owner",
                email="other@example.test",
                password_hash="not-a-real-password-hash",
                role="owner",
            )
            db.add_all([self.owner, self.member, self.other_owner])
            db.commit()
            self.owner_id = self.owner.id
            self.member_id = self.member.id
            self.tenant_id = tenant.id
            self.other_owner_id = self.other_owner.id
            self.other_tenant_id = other_tenant.id

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
        self.other_owner_headers = self._headers(
            self.other_owner_id, self.other_tenant_id, "owner"
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

    def _audit_items(self, **params):
        response = self.client.get(
            "/audit-logs", headers=self.owner_headers, params=params
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def test_records_supported_user_project_and_task_operations(self) -> None:
        created_member = self.client.post(
            "/users/members",
            headers=self.owner_headers,
            json={
                "name": "New Member",
                "email": "new@example.test",
                "password": "do-not-audit-this-password",
                "role": "member",
            },
        )
        self.assertEqual(created_member.status_code, 200, created_member.text)
        new_member_id = created_member.json()["id"]

        role_update = self.client.patch(
            f"/users/members/{new_member_id}/role",
            headers=self.owner_headers,
            json={"role": "manager"},
        )
        self.assertEqual(role_update.status_code, 200, role_update.text)
        repeated_role_update = self.client.patch(
            f"/users/members/{new_member_id}/role",
            headers=self.owner_headers,
            json={"role": "manager"},
        )
        self.assertEqual(repeated_role_update.status_code, 200)

        project = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={"name": "Project One", "description": "private text"},
        )
        self.assertEqual(project.status_code, 200, project.text)
        project_id = project.json()["id"]

        updated_project = self.client.put(
            f"/projects/{project_id}",
            headers=self.owner_headers,
            json={"name": "Project Updated", "description": "private text"},
        )
        self.assertEqual(updated_project.status_code, 200, updated_project.text)
        repeated_project_update = self.client.put(
            f"/projects/{project_id}",
            headers=self.owner_headers,
            json={"name": "Project Updated", "description": "private text"},
        )
        self.assertEqual(repeated_project_update.status_code, 200)

        task = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={
                "project_id": project_id,
                "title": "Task One",
            },
        )
        self.assertEqual(task.status_code, 200, task.text)
        task_id = task.json()["id"]

        changed_task = self.client.put(
            f"/tasks/{task_id}",
            headers=self.owner_headers,
            json={
                "title": "Task Updated",
                "status": "in_progress",
                "assigned_to": self.member_id,
            },
        )
        self.assertEqual(changed_task.status_code, 200, changed_task.text)
        repeated_task_update = self.client.put(
            f"/tasks/{task_id}",
            headers=self.owner_headers,
            json={
                "title": "Task Updated",
                "status": "in_progress",
                "assigned_to": self.member_id,
            },
        )
        self.assertEqual(repeated_task_update.status_code, 200)

        deleted_task = self.client.delete(
            f"/tasks/{task_id}", headers=self.owner_headers
        )
        self.assertEqual(deleted_task.status_code, 200, deleted_task.text)
        deleted_project = self.client.delete(
            f"/projects/{project_id}", headers=self.owner_headers
        )
        self.assertEqual(deleted_project.status_code, 200, deleted_project.text)

        rows = self._audit_items(page_size=100)
        actions = [row["action"] for row in rows]
        self.assertCountEqual(
            actions,
            [
                "member_created",
                "role_updated",
                "project_created",
                "project_updated",
                "task_created",
                "task_assigned",
                "task_updated",
                "task_status_changed",
                "task_deleted",
                "project_deleted",
            ],
        )

        by_action = {row["action"]: row for row in rows}
        for action in ("member_created", "role_updated", "project_created",
                       "project_updated", "task_created", "task_assigned",
                       "task_updated", "task_status_changed", "task_deleted",
                       "project_deleted"):
            self.assertEqual(by_action[action]["user_id"], self.owner_id)
            self.assertEqual(by_action[action]["tenant_id"], self.tenant_id)
        self.assertEqual(by_action["member_created"]["resource_id"], new_member_id)
        self.assertEqual(by_action["project_created"]["resource_id"], project_id)
        self.assertEqual(by_action["task_created"]["resource_id"], task_id)
        self.assertEqual(by_action["task_status_changed"]["metadata"], {
            "old_status": "todo",
            "new_status": "in_progress",
        })
        self.assertEqual(by_action["task_updated"]["metadata"], {
            "changed_fields": ["title"],
        })
        self.assertEqual(by_action["task_assigned"]["metadata"], {
            "old_assigned_to": None,
            "new_assigned_to": self.member_id,
        })
        serialized = str(rows)
        self.assertNotIn("do-not-audit-this-password", serialized)
        self.assertNotIn("private text", serialized)

    def test_registration_is_audited_with_new_user_as_actor(self) -> None:
        response = self.client.post(
            "/users/register",
            json={
                "company_name": "New Tenant",
                "name": "New Owner",
                "email": "new-owner@example.test",
                "password": "never-log-this-password",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        user_id = response.json()["id"]
        with self.session_factory() as db:
            audit = db.query(AuditLog).filter_by(action="user_created").one()
            self.assertEqual(audit.user_id, user_id)
            self.assertEqual(audit.resource_id, user_id)
            self.assertEqual(audit.tenant_id, response.json()["tenant_id"])
            self.assertNotIn("never-log-this-password", str(audit.metadata_json))

    def test_authentication_role_and_tenant_isolation(self) -> None:
        no_token = self.client.get("/audit-logs")
        self.assertIn(no_token.status_code, (401, 403))

        member_response = self.client.get(
            "/audit-logs", headers=self.member_headers
        )
        self.assertEqual(member_response.status_code, 403)

        with self.session_factory() as db:
            db.add(
                AuditLog(
                    tenant_id=self.other_tenant_id,
                    user_id=self.other_owner_id,
                    action="other_tenant_event",
                    resource_type="project",
                    resource_id=100,
                )
            )
            db.commit()

        own = self.client.get("/audit-logs", headers=self.owner_headers)
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["total"], 0)
        foreign_actor_filter = self.client.get(
            "/audit-logs",
            headers=self.owner_headers,
            params={"user_id": self.other_owner_id},
        )
        self.assertEqual(foreign_actor_filter.status_code, 200)
        self.assertEqual(foreign_actor_filter.json()["total"], 0)

        with self.session_factory() as db:
            owner = db.query(User).filter_by(id=self.owner_id).one()
            owner.role = "member"
            db.commit()
        stale_owner_token = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={"name": "Must Not Be Created"},
        )
        self.assertEqual(stale_owner_token.status_code, 403)

    def test_filters_and_pagination(self) -> None:
        with self.session_factory() as db:
            now = datetime.now(timezone.utc)
            for index in range(3):
                db.add(
                    AuditLog(
                        tenant_id=self.tenant_id,
                        user_id=self.owner_id,
                        action="project_created" if index < 2 else "task_updated",
                        resource_type="project" if index < 2 else "task",
                        resource_id=index + 1,
                        created_at=now + timedelta(seconds=index),
                    )
                )
            db.commit()

        page = self.client.get(
            "/audit-logs",
            headers=self.owner_headers,
            params={"action": "project_created", "resource_type": "project",
                    "user_id": self.owner_id, "page": 2, "page_size": 1},
        )
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["total"], 2)
        self.assertEqual(page.json()["total_pages"], 2)
        self.assertEqual(page.json()["page"], 2)
        self.assertEqual(len(page.json()["items"]), 1)

        date_filtered = self.client.get(
            "/audit-logs",
            headers=self.owner_headers,
            params={
                "start_date": (now + timedelta(seconds=1)).isoformat(),
                "end_date": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        self.assertEqual(date_filtered.status_code, 200, date_filtered.text)
        self.assertEqual(date_filtered.json()["total"], 1)

    def test_failed_operation_and_audit_failure_do_not_record_success(self) -> None:
        invalid_task = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={"project_id": 99999, "title": "Not created"},
        )
        self.assertEqual(invalid_task.status_code, 404)
        self.assertEqual(self._audit_items(), [])

        def fail_audit_flush(_session, _flush_context, instances):
            del _session, _flush_context
            if any(isinstance(instance, AuditLog) for instance in instances):
                raise RuntimeError("forced audit insert failure")

        event.listen(Session, "before_flush", fail_audit_flush)
        try:
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/projects/",
                headers=self.owner_headers,
                json={"name": "Must Roll Back"},
            )
            self.assertEqual(response.status_code, 500)
        finally:
            event.remove(Session, "before_flush", fail_audit_flush)

        with self.session_factory() as db:
            self.assertIsNone(
                db.query(AuditLog).filter_by(action="project_created").first()
            )
            self.assertFalse(
                any(
                    project.name == "Must Roll Back"
                    for project in db.query(Project).all()
                )
            )

    def test_cross_tenant_assignee_is_rejected_without_audit(self) -> None:
        with self.session_factory() as db:
            project = Project(tenant_id=self.tenant_id, name="Tenant Project")
            db.add(project)
            db.commit()
            project_id = project.id

        response = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={
                "project_id": project_id,
                "title": "Invalid assignment",
                "assigned_to": self.other_owner_id,
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._audit_items(), [])

    def test_audit_metadata_drops_sensitive_keys(self) -> None:
        with self.session_factory() as db:
            create_audit_log(
                db=db,
                tenant_id=self.tenant_id,
                user_id=self.owner_id,
                action="test_event",
                resource_type="user",
                metadata={
                    "old_role": "member",
                    "new_role": "manager",
                    "password": "do-not-store",
                    "api_key": "do-not-store",
                    "unrecognized": "do-not-store",
                },
            )
            db.commit()
            row = db.query(AuditLog).filter_by(action="test_event").one()
            self.assertEqual(
                row.metadata_json,
                {"old_role": "member", "new_role": "manager"},
            )
            self.assertNotIn("do-not-store", str(row.metadata_json))


if __name__ == "__main__":
    unittest.main()
