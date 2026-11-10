import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "document-api-test-signing-key"
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import get_db
from backend.app.main import app
from backend.app.models import AuditLog, Base, Document, Project, Tenant, User
from backend.app.services.s3_storage import (
    ObjectMetadataMismatchError,
    ObjectNotFoundError,
    StorageConfigurationError,
    StorageServiceError,
    StoredObjectMetadata,
)
from backend.app.utils.jwt import create_access_token


class DocumentRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upload_key_counter = 0
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        with self.session_factory() as db:
            tenant = Tenant(name="Document API tenant")
            other_tenant = Tenant(name="Foreign document tenant")
            db.add_all([tenant, other_tenant])
            db.flush()
            self.tenant_id = tenant.id
            self.other_tenant_id = other_tenant.id
            self.owner = User(
                tenant_id=tenant.id,
                name="Owner",
                email="document-api-owner@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            self.member = User(
                tenant_id=tenant.id,
                name="Member",
                email="document-api-member@example.test",
                password_hash="not-a-real-hash",
                role="member",
            )
            self.foreign_owner = User(
                tenant_id=other_tenant.id,
                name="Foreign owner",
                email="document-api-foreign@example.test",
                password_hash="not-a-real-hash",
                role="owner",
            )
            self.project = Project(tenant_id=tenant.id, name="Tenant project")
            self.foreign_project = Project(
                tenant_id=other_tenant.id, name="Foreign project"
            )
            db.add_all(
                [
                    self.owner,
                    self.member,
                    self.foreign_owner,
                    self.project,
                    self.foreign_project,
                ]
            )
            db.commit()
            self.owner_id = self.owner.id
            self.member_id = self.member.id
            self.foreign_owner_id = self.foreign_owner.id
            self.project_id = self.project.id
            self.foreign_project_id = self.foreign_project.id

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.owner_headers = self._headers(self.owner_id, self.tenant_id)
        self.member_headers = self._headers(self.member_id, self.tenant_id)
        self.foreign_headers = self._headers(
            self.foreign_owner_id, self.other_tenant_id
        )

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        del _record
        connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _headers(user_id: int, tenant_id: int) -> dict[str, str]:
        token = create_access_token(
            {"sub": str(user_id), "tenant_id": tenant_id, "role": "owner"}
        )
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _upload_body(**changes) -> dict:
        body = {
            "original_filename": "project-plan.pdf",
            "content_type": "application/pdf",
            "size_bytes": 2048,
        }
        body.update(changes)
        return body

    def _unique_signed_upload(self, *_args, **_kwargs) -> dict:
        self.upload_key_counter += 1
        key = f"documents/{self.upload_key_counter:032x}"
        return {
            "object_key": key,
            "url": "https://s3.example.test/upload?signature=do-not-log",
            "headers": {
                "Content-Type": "application/pdf",
                "x-amz-server-side-encryption": "AES256",
            },
        }

    def _create_document(self, *, status_value="pending_upload") -> dict:
        with patch(
            "backend.app.routes.documents_routes.s3_storage.create_upload_url",
            side_effect=self._unique_signed_upload,
        ):
            response = self.client.post(
                f"/projects/{self.project_id}/documents",
                headers=self.owner_headers,
                json=self._upload_body(),
            )
        self.assertEqual(response.status_code, 200, response.text)
        if status_value == "available":
            with self.session_factory() as db:
                document = db.query(Document).filter_by(id=response.json()["document"]["id"]).one()
                document.status = "available"
                db.commit()
        return response.json()

    def _get_audit_events(self, action: str) -> list[AuditLog]:
        with self.session_factory() as db:
            return db.query(AuditLog).filter(AuditLog.action == action).all()

    def test_initiate_upload_creates_pending_record_and_safe_audit(self) -> None:
        with patch(
            "backend.app.routes.documents_routes.s3_storage.create_upload_url",
            side_effect=self._unique_signed_upload,
        ) as create_upload:
            response = self.client.post(
                f"/projects/{self.project_id}/documents",
                headers=self.owner_headers,
                json=self._upload_body(),
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["document"]["status"], "pending_upload")
        self.assertEqual(payload["document"]["project_id"], self.project_id)
        self.assertNotIn("storage_key", payload["document"])
        self.assertEqual(
            payload["upload_url"],
            "https://s3.example.test/upload?signature=do-not-log",
        )
        self.assertEqual(payload["expires_in"], 300)
        create_upload.assert_called_once_with("application/pdf")
        events = self._get_audit_events("document_upload_initiated")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tenant_id, self.tenant_id)
        self.assertEqual(events[0].user_id, self.owner_id)
        self.assertEqual(events[0].resource_id, payload["document"]["id"])
        self.assertNotIn(
            "signature",
            str(events[0].metadata_json),
        )
        self.assertNotIn("original_filename", events[0].metadata_json)

    def test_validation_rejects_bad_filename_type_and_size(self) -> None:
        bad_inputs = (
            self._upload_body(original_filename="../private.pdf"),
            self._upload_body(original_filename="folder\\private.pdf"),
            self._upload_body(original_filename="bad\nname.pdf"),
            self._upload_body(content_type="text/html"),
            self._upload_body(size_bytes=0),
            self._upload_body(size_bytes=25 * 1024 * 1024 + 1),
            {**self._upload_body(), "unexpected": "field"},
        )
        with patch(
            "backend.app.routes.documents_routes.s3_storage.create_upload_url"
        ) as create_upload:
            for body in bad_inputs:
                with self.subTest(body=body):
                    response = self.client.post(
                        f"/projects/{self.project_id}/documents",
                        headers=self.owner_headers,
                        json=body,
                    )
                    self.assertEqual(response.status_code, 422)
            create_upload.assert_not_called()
        with self.session_factory() as db:
            self.assertEqual(db.query(Document).count(), 0)

    def test_permissions_and_cross_tenant_project_isolation(self) -> None:
        denied = self.client.post(
            f"/projects/{self.project_id}/documents",
            headers=self.member_headers,
            json=self._upload_body(),
        )
        self.assertEqual(denied.status_code, 403)

        with patch(
            "backend.app.routes.documents_routes.s3_storage.create_upload_url"
        ) as create_upload:
            foreign = self.client.post(
                f"/projects/{self.foreign_project_id}/documents",
                headers=self.owner_headers,
                json=self._upload_body(),
            )
            create_upload.assert_not_called()
        self.assertEqual(foreign.status_code, 404)

        no_token = self.client.get(
            f"/projects/{self.project_id}/documents"
        )
        self.assertIn(no_token.status_code, (401, 403))

    def test_missing_storage_configuration_returns_safe_error(self) -> None:
        with patch(
            "backend.app.routes.documents_routes.s3_storage.create_upload_url",
            side_effect=StorageConfigurationError("raw config detail"),
        ):
            response = self.client.post(
                f"/projects/{self.project_id}/documents",
                headers=self.owner_headers,
                json=self._upload_body(),
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("raw config detail", response.text)
        with self.session_factory() as db:
            self.assertEqual(db.query(Document).count(), 0)
        self.assertEqual(self._get_audit_events("document_upload_initiated"), [])

    def test_database_failure_does_not_leave_record_or_success_audit(self) -> None:
        with (
            patch(
                "backend.app.routes.documents_routes.s3_storage.create_upload_url",
                side_effect=self._unique_signed_upload,
            ),
            patch(
                "sqlalchemy.orm.Session.commit",
                side_effect=SQLAlchemyError("private database detail"),
            ),
        ):
            response = self.client.post(
                f"/projects/{self.project_id}/documents",
                headers=self.owner_headers,
                json=self._upload_body(),
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private database detail", response.text)
        with self.session_factory() as db:
            self.assertEqual(db.query(Document).count(), 0)
            self.assertEqual(
                db.query(AuditLog)
                .filter(AuditLog.action == "document_upload_initiated")
                .count(),
                0,
            )

    def test_completion_success_updates_status_and_audits(self) -> None:
        created = self._create_document()
        document_id = created["document"]["id"]
        stored_metadata = StoredObjectMetadata(
            content_length=2048,
            content_type="application/pdf",
            etag='"etag"',
            checksum_sha256="sha256-base64=",
            server_side_encryption="AES256",
            kms_key_id=None,
        )
        with patch(
            "backend.app.routes.documents_routes.s3_storage.verify_uploaded_object",
            return_value=stored_metadata,
        ) as verify:
            response = self.client.post(
                f"/projects/{self.project_id}/documents/{document_id}/complete",
                headers=self.owner_headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "available")
        self.assertEqual(response.json()["checksum"], "sha256-base64=")
        self.assertNotIn("storage_key", response.json())
        verify.assert_called_once_with(
            f"documents/{1:032x}",
            "application/pdf",
            2048,
        )
        events = self._get_audit_events("document_upload_completed")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].user_id, self.owner_id)

    def test_incomplete_upload_stays_pending_without_success_audit(self) -> None:
        created = self._create_document()
        document_id = created["document"]["id"]

        for error, expected_status in (
            (ObjectNotFoundError("missing"), 409),
            (ObjectMetadataMismatchError("mismatch"), 409),
            (StorageServiceError("aws detail"), 503),
        ):
            with self.subTest(error=type(error).__name__):
                with patch(
                    "backend.app.routes.documents_routes.s3_storage.verify_uploaded_object",
                    side_effect=error,
                ):
                    response = self.client.post(
                        f"/projects/{self.project_id}/documents/{document_id}/complete",
                        headers=self.owner_headers,
                    )
                self.assertEqual(response.status_code, expected_status)
                self.assertNotIn("aws detail", response.text)
                with self.session_factory() as db:
                    document = db.get(Document, document_id)
                    self.assertEqual(document.status, "pending_upload")
        self.assertEqual(
            self._get_audit_events("document_upload_completed"), []
        )

    def test_completion_is_tenant_scoped_and_requires_write_role(self) -> None:
        created = self._create_document()
        document_id = created["document"]["id"]
        with patch(
            "backend.app.routes.documents_routes.s3_storage.verify_uploaded_object"
        ) as verify:
            denied = self.client.post(
                f"/projects/{self.project_id}/documents/{document_id}/complete",
                headers=self.member_headers,
            )
            self.assertEqual(denied.status_code, 403)

            foreign = self.client.post(
                f"/projects/{self.project_id}/documents/{document_id}/complete",
                headers=self.foreign_headers,
            )
            self.assertEqual(foreign.status_code, 404)
            verify.assert_not_called()

    def test_list_and_metadata_are_paginated_and_tenant_scoped(self) -> None:
        self._create_document()
        self._create_document()
        response = self.client.get(
            f"/projects/{self.project_id}/documents?page=1&page_size=1",
            headers=self.member_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["page_size"], 1)
        self.assertEqual(len(payload["items"]), 1)
        self.assertNotIn("storage_key", payload["items"][0])

        second_page = self.client.get(
            f"/projects/{self.project_id}/documents?page=2&page_size=1",
            headers=self.member_headers,
        )
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.json()["page"], 2)
        self.assertEqual(len(second_page.json()["items"]), 1)

        document_id = payload["items"][0]["id"]
        metadata_response = self.client.get(
            f"/projects/{self.project_id}/documents/{document_id}",
            headers=self.member_headers,
        )
        self.assertEqual(metadata_response.status_code, 200)
        self.assertNotIn("storage_key", metadata_response.json())

        foreign_response = self.client.get(
            f"/projects/{self.project_id}/documents/{document_id}",
            headers=self.foreign_headers,
        )
        self.assertEqual(foreign_response.status_code, 404)
        foreign_project_document_response = self.client.get(
            f"/projects/{self.foreign_project_id}/documents/{document_id}",
            headers=self.foreign_headers,
        )
        self.assertEqual(foreign_project_document_response.status_code, 404)

        with self.session_factory() as db:
            actions = {
                row.action
                for row in db.query(AuditLog)
                .filter(AuditLog.tenant_id == self.tenant_id)
                .all()
            }
        self.assertIn("documents_listed", actions)
        self.assertIn("document_viewed", actions)

    def test_authorized_download_requires_available_object(self) -> None:
        created = self._create_document(status_value="available")
        document_id = created["document"]["id"]
        metadata = StoredObjectMetadata(
            content_length=2048,
            content_type="application/pdf",
            etag='"etag"',
            checksum_sha256=None,
            server_side_encryption="AES256",
            kms_key_id=None,
        )
        with (
            patch(
                "backend.app.routes.documents_routes.s3_storage.get_object_metadata",
                return_value=metadata,
            ) as head,
            patch(
                "backend.app.routes.documents_routes.s3_storage.create_download_url",
                return_value="https://s3.example.test/download?secret=do-not-log",
            ) as create_url,
        ):
            response = self.client.post(
                f"/projects/{self.project_id}/documents/{document_id}/download-url",
                headers=self.member_headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["expires_in"], 300)
        head.assert_called_once_with(f"documents/{1:032x}")
        create_url.assert_called_once_with(f"documents/{1:032x}")

        event = self._get_audit_events("document_download_url_created")[0]
        self.assertNotIn("secret", str(event.metadata_json))
        self.assertNotIn("download_url", event.metadata_json)

    def test_download_errors_and_unavailable_document_do_not_issue_url(self) -> None:
        created = self._create_document()
        document_id = created["document"]["id"]
        with patch(
            "backend.app.routes.documents_routes.s3_storage.get_object_metadata"
        ) as head:
            pending = self.client.post(
                f"/projects/{self.project_id}/documents/{document_id}/download-url",
                headers=self.member_headers,
            )
            self.assertEqual(pending.status_code, 409)
            head.assert_not_called()

        available = self._create_document(status_value="available")
        with patch(
            "backend.app.routes.documents_routes.s3_storage.get_object_metadata",
            side_effect=StorageServiceError("provider details"),
        ):
            unavailable = self.client.post(
                f"/projects/{self.project_id}/documents/{available['document']['id']}/download-url",
                headers=self.member_headers,
            )
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn("provider details", unavailable.text)
        self.assertEqual(
            self._get_audit_events("document_download_url_created"), []
        )

    def test_soft_delete_is_authorized_audited_and_idempotent(self) -> None:
        created = self._create_document(status_value="available")
        document_id = created["document"]["id"]
        with patch(
            "backend.app.routes.documents_routes.enqueue_document_cleanup",
            return_value=True,
        ) as enqueue:
            response = self.client.delete(
                f"/projects/{self.project_id}/documents/{document_id}",
                headers=self.owner_headers,
            )
            duplicate = self.client.delete(
                f"/projects/{self.project_id}/documents/{document_id}",
                headers=self.owner_headers,
            )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "deletion_pending")
        self.assertTrue(response.json()["cleanup_queued"])
        self.assertEqual(duplicate.status_code, 202)
        self.assertFalse(duplicate.json()["cleanup_queued"])
        enqueue.assert_called_once_with(self.tenant_id, document_id)
        with self.session_factory() as db:
            document = db.get(Document, document_id)
            self.assertEqual(document.status, "deletion_pending")
            self.assertIsNotNone(document.deleted_at)
            self.assertEqual(document.deletion_requested_by, self.owner_id)
            self.assertEqual(
                db.query(AuditLog)
                .filter(
                    AuditLog.action == "document_deletion_requested",
                    AuditLog.resource_id == document_id,
                )
                .count(),
                1,
            )

    def test_soft_delete_permission_and_tenant_checks(self) -> None:
        created = self._create_document()
        document_id = created["document"]["id"]
        with patch(
            "backend.app.routes.documents_routes.enqueue_document_cleanup"
        ) as enqueue:
            member_response = self.client.delete(
                f"/projects/{self.project_id}/documents/{document_id}",
                headers=self.member_headers,
            )
            foreign_response = self.client.delete(
                f"/projects/{self.project_id}/documents/{document_id}",
                headers=self.foreign_headers,
            )
            wrong_project_response = self.client.delete(
                f"/projects/{self.foreign_project_id}/documents/{document_id}",
                headers=self.foreign_headers,
            )
        self.assertEqual(member_response.status_code, 403)
        self.assertEqual(foreign_response.status_code, 404)
        self.assertEqual(wrong_project_response.status_code, 404)
        enqueue.assert_not_called()
        self.assertEqual(
            self._get_audit_events("document_deletion_requested"), []
        )

    def test_broker_failure_keeps_soft_deleted_document_recoverable(self) -> None:
        created = self._create_document(status_value="available")
        document_id = created["document"]["id"]
        with patch(
            "backend.app.routes.documents_routes.enqueue_document_cleanup",
            return_value=False,
        ):
            response = self.client.delete(
                f"/projects/{self.project_id}/documents/{document_id}",
                headers=self.owner_headers,
            )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["cleanup_queued"])
        with self.session_factory() as db:
            document = db.get(Document, document_id)
            self.assertEqual(document.status, "deletion_pending")
            self.assertIsNotNone(document.deleted_at)
            self.assertEqual(
                db.query(AuditLog)
                .filter(
                    AuditLog.action == "document_deletion_requested",
                    AuditLog.resource_id == document_id,
                )
                .count(),
                1,
            )


if __name__ == "__main__":
    unittest.main()
