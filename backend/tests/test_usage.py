import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "usage-test-signing-key"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import Base, Plan, Project, Subscription, Tenant, User
from backend.app.utils.jwt import create_access_token


class UsageQuotaTests(unittest.TestCase):
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
            limited_plan = Plan(
                code="limited",
                name="Limited",
                max_users=2,
                max_projects=1,
                max_tasks=1,
                max_documents=2,
                max_storage_bytes=100,
            )
            unlimited_plan = Plan(code="unlimited", name="Unlimited")
            db.add_all([limited_plan, unlimited_plan])
            db.flush()
            self.tenant, self.owner = self._create_user(
                db, "Limited Tenant", "owner"
            )
            self.other_tenant, self.other_owner = self._create_user(
                db, "Other Tenant", "owner"
            )
            self._create_subscription(db, self.tenant.id, limited_plan.id)
            self._create_subscription(
                db, self.other_tenant.id, unlimited_plan.id
            )
            db.commit()
            self.owner_id = self.owner.id
            self.tenant_id = self.tenant.id
            self.other_owner_id = self.other_owner.id
            self.other_tenant_id = self.other_tenant.id

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
        self.other_owner_headers = self._headers(
            self.other_owner_id, self.other_tenant_id, "owner"
        )

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _create_user(db, tenant_name: str, role: str):
        tenant = Tenant(name=tenant_name)
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id,
            name=f"{role} user",
            email=f"{tenant_name}-{role}@example.test",
            password_hash="not-a-real-password-hash",
            role=role,
        )
        db.add(user)
        db.flush()
        return tenant, user

    @staticmethod
    def _create_subscription(db, tenant_id: int, plan_id: int) -> None:
        start = datetime.now(timezone.utc)
        db.add(
            Subscription(
                tenant_id=tenant_id,
                plan_id=plan_id,
                current_period_start=start,
                current_period_end=start + timedelta(days=30),
            )
        )

    @staticmethod
    def _headers(user_id: int, tenant_id: int, role: str) -> dict[str, str]:
        token = create_access_token(
            {"sub": str(user_id), "tenant_id": tenant_id, "role": role}
        )
        return {"Authorization": f"Bearer {token}"}

    def test_usage_counts_are_tenant_scoped_and_limits_are_returned(self):
        response = self.client.get(
            "/subscription/usage", headers=self.owner_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["users"], {"used": 1, "limit": 2})
        self.assertEqual(
            response.json()["storage_bytes"], {"used": 0, "limit": 100}
        )

        other_response = self.client.get(
            "/subscription/usage", headers=self.other_owner_headers
        )
        self.assertEqual(other_response.status_code, 200)
        self.assertEqual(
            other_response.json()["projects"], {"used": 0, "limit": None}
        )

        forged_headers = self._headers(
            self.owner_id, self.other_tenant_id, "owner"
        )
        forged = self.client.get(
            "/subscription/usage", headers=forged_headers
        )
        self.assertEqual(forged.status_code, 401)

    def test_missing_or_inactive_subscription_fails_closed(self):
        with self.session_factory() as db:
            tenant, user = self._create_user(db, "Missing Subscription", "owner")
            db.commit()
            missing_headers = self._headers(user.id, tenant.id, "owner")

        missing = self.client.post(
            "/projects/",
            headers=missing_headers,
            json={"name": "Blocked project"},
        )
        self.assertEqual(missing.status_code, 403)
        self.assertIn("active subscription is required", missing.json()["detail"])

        with self.session_factory() as db:
            subscription = (
                db.query(Subscription)
                .filter(Subscription.tenant_id == self.tenant_id)
                .one()
            )
            subscription.status = "canceled"
            db.commit()

        inactive = self.client.get(
            "/subscription/usage", headers=self.owner_headers
        )
        self.assertEqual(inactive.status_code, 403)
        self.assertIn("active subscription is required", inactive.json()["detail"])

    def test_user_project_and_task_quotas_are_enforced(self):
        first_member = self.client.post(
            "/users/members",
            headers=self.owner_headers,
            json={
                "name": "Member",
                "email": "member@example.test",
                "password": "password123",
                "role": "member",
            },
        )
        self.assertEqual(first_member.status_code, 200, first_member.text)

        second_member = self.client.post(
            "/users/members",
            headers=self.owner_headers,
            json={
                "name": "Second Member",
                "email": "second-member@example.test",
                "password": "password123",
                "role": "member",
            },
        )
        self.assertEqual(second_member.status_code, 403)
        self.assertIn("users quota exceeded", second_member.json()["detail"])

        project = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={"name": "First project"},
        )
        self.assertEqual(project.status_code, 200, project.text)
        project_id = project.json()["id"]

        second_project = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={"name": "Second project"},
        )
        self.assertEqual(second_project.status_code, 403)
        self.assertIn("projects quota exceeded", second_project.json()["detail"])

        task = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={"project_id": project_id, "title": "First task"},
        )
        self.assertEqual(task.status_code, 200, task.text)

        second_task = self.client.post(
            "/tasks/",
            headers=self.owner_headers,
            json={"project_id": project_id, "title": "Second task"},
        )
        self.assertEqual(second_task.status_code, 403)
        self.assertIn("tasks quota exceeded", second_task.json()["detail"])

    def test_document_and_storage_quotas_are_enforced_before_upload(self):
        project = self.client.post(
            "/projects/",
            headers=self.owner_headers,
            json={"name": "Document project"},
        )
        self.assertEqual(project.status_code, 200, project.text)
        project_id = project.json()["id"]
        upload = {
            "original_filename": "one.txt",
            "content_type": "text/plain",
            "size_bytes": 60,
        }
        signed_upload = {
            "object_key": "tenant/document-one",
            "url": "https://uploads.example.test/one",
            "headers": {"Content-Type": "text/plain"},
        }
        with patch(
            "backend.app.routes.documents_routes.s3_storage.create_upload_url",
            return_value=signed_upload,
        ) as create_upload_url:
            first = self.client.post(
                f"/projects/{project_id}/documents",
                headers=self.owner_headers,
                json=upload,
            )
            self.assertEqual(first.status_code, 200, first.text)

            storage_exceeded = self.client.post(
                f"/projects/{project_id}/documents",
                headers=self.owner_headers,
                json={
                    **upload,
                    "original_filename": "two.txt",
                    "size_bytes": 50,
                },
            )
            self.assertEqual(storage_exceeded.status_code, 403)
            self.assertIn(
                "storage_bytes quota exceeded",
                storage_exceeded.json()["detail"],
            )
            create_upload_url.assert_called_once()

        with self.session_factory() as db:
            plan = db.query(Plan).filter(Plan.code == "limited").one()
            plan.max_storage_bytes = 1000
            db.commit()

        with patch(
            "backend.app.routes.documents_routes.s3_storage.create_upload_url",
            return_value={
                **signed_upload,
                "object_key": "tenant/document-two",
            },
        ):
            second_document = self.client.post(
                f"/projects/{project_id}/documents",
                headers=self.owner_headers,
                json={
                    **upload,
                    "original_filename": "two.txt",
                    "size_bytes": 1,
                },
            )
        self.assertEqual(second_document.status_code, 200, second_document.text)

        document_exceeded = self.client.post(
            f"/projects/{project_id}/documents",
            headers=self.owner_headers,
            json={
                **upload,
                "original_filename": "three.txt",
                "size_bytes": 1,
            },
        )
        self.assertEqual(document_exceeded.status_code, 403)
        self.assertIn(
            "documents quota exceeded",
            document_exceeded.json()["detail"],
        )


if __name__ == "__main__":
    unittest.main()
