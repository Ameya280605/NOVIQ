import os
import unittest

os.environ["DATABASE_URL"] = "sqlite://"

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, Document, Project, Tenant, User
from backend.app.schemas.documents import DocumentMetadata
from backend.app.utils.documents import (
    get_tenant_document,
    get_tenant_project,
    require_document_delete_access,
    require_document_read_access,
)


class DocumentModelAndAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

        tenant = Tenant(name="Documents tenant")
        other_tenant = Tenant(name="Other documents tenant")
        self.db.add_all([tenant, other_tenant])
        self.db.flush()
        self.tenant = tenant
        self.other_tenant = other_tenant

        self.owner = User(
            tenant_id=tenant.id,
            name="Owner",
            email="documents-owner@example.test",
            password_hash="test-hash",
            role="owner",
        )
        self.member = User(
            tenant_id=tenant.id,
            name="Member",
            email="documents-member@example.test",
            password_hash="test-hash",
            role="member",
        )
        self.foreign_owner = User(
            tenant_id=other_tenant.id,
            name="Foreign owner",
            email="documents-foreign-owner@example.test",
            password_hash="test-hash",
            role="owner",
        )
        self.project = Project(tenant_id=tenant.id, name="Tenant project")
        self.foreign_project = Project(
            tenant_id=other_tenant.id, name="Foreign project"
        )
        self.db.add_all(
            [
                self.owner,
                self.member,
                self.foreign_owner,
                self.project,
                self.foreign_project,
            ]
        )
        self.db.flush()
        self.document = self._new_document()
        self.db.add(self.document)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        del _record
        connection.execute("PRAGMA foreign_keys=ON")

    def _new_document(self, **overrides) -> Document:
        values = {
            "tenant_id": self.tenant.id,
            "project_id": self.project.id,
            "uploaded_by": self.owner.id,
            "original_filename": "spec.pdf",
            "storage_key": "tenant/project/document/spec",
            "content_type": "application/pdf",
            "size_bytes": 128,
        }
        values.update(overrides)
        return Document(**values)

    def test_document_relationships_and_default_lifecycle(self) -> None:
        self.db.refresh(self.document)
        self.assertEqual(self.document.status, "pending_upload")
        self.assertIsNotNone(self.document.created_at)
        self.assertIsNotNone(self.document.updated_at)
        self.assertIsNone(self.document.deleted_at)
        self.assertEqual(self.document.tenant.id, self.tenant.id)
        self.assertEqual(self.document.project.id, self.project.id)
        self.assertEqual(self.document.uploader.id, self.owner.id)
        self.assertIn(self.document, self.tenant.documents)
        self.assertIn(self.document, self.project.documents)
        self.assertIn(self.document, self.owner.uploaded_documents)

    def test_database_rejects_tenant_project_mismatch(self) -> None:
        invalid_document = self._new_document(
            storage_key="mismatched/tenant/project",
            project_id=self.foreign_project.id,
        )
        self.db.add(invalid_document)
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_database_rejects_tenant_uploader_mismatch(self) -> None:
        invalid_document = self._new_document(
            storage_key="mismatched/tenant/uploader",
            uploaded_by=self.foreign_owner.id,
        )
        self.db.add(invalid_document)
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_database_rejects_invalid_status_and_negative_size(self) -> None:
        invalid_status = self._new_document(
            storage_key="invalid/status", status="uploaded"
        )
        self.db.add(invalid_status)
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

        negative_size = self._new_document(
            storage_key="invalid/size", size_bytes=-1
        )
        self.db.add(negative_size)
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_metadata_schema_validates_and_hides_storage_key(self) -> None:
        metadata = DocumentMetadata.model_validate(self.document)
        self.assertEqual(metadata.original_filename, "spec.pdf")
        self.assertNotIn("storage_key", metadata.model_dump())

        with self.assertRaises(ValidationError):
            DocumentMetadata.model_validate(
                {
                    "id": 3,
                    "project_id": self.project.id,
                    "uploaded_by": self.owner.id,
                    "original_filename": " ",
                    "content_type": "application/pdf",
                    "size_bytes": 10,
                    "checksum": None,
                    "status": "unknown",
                    "created_at": self.document.created_at,
                    "updated_at": self.document.updated_at,
                    "deleted_at": None,
                }
            )

    def test_tenant_scoped_helpers_hide_other_tenants_resources(self) -> None:
        self.assertEqual(
            get_tenant_project(self.db, self.project.id, self.tenant.id).id,
            self.project.id,
        )
        self.assertEqual(
            get_tenant_document(
                self.db, self.document.id, self.tenant.id
            ).id,
            self.document.id,
        )

        with self.assertRaises(HTTPException) as project_error:
            get_tenant_project(self.db, self.project.id, self.other_tenant.id)
        self.assertEqual(project_error.exception.status_code, 404)

        with self.assertRaises(HTTPException) as document_error:
            get_tenant_document(
                self.db, self.document.id, self.other_tenant.id
            )
        self.assertEqual(document_error.exception.status_code, 404)

    def test_document_lookup_can_require_project_scope_and_hides_deleted(self) -> None:
        with self.assertRaises(HTTPException):
            get_tenant_document(
                self.db,
                self.document.id,
                self.tenant.id,
                project_id=self.foreign_project.id,
            )

        self.document.deleted_at = self.document.created_at
        self.db.commit()
        with self.assertRaises(HTTPException):
            get_tenant_document(
                self.db, self.document.id, self.tenant.id
            )

    def test_role_helper_uses_database_role_and_tenant(self) -> None:
        member_claim = {
            "sub": str(self.member.id),
            "tenant_id": self.tenant.id,
            "role": "owner",
        }
        verified_member = require_document_read_access(
            current_user=member_claim, db=self.db
        )
        self.assertEqual(verified_member["role"], "member")

        with self.assertRaises(HTTPException) as role_error:
            require_document_delete_access(
                current_user=member_claim, db=self.db
            )
        self.assertEqual(role_error.exception.status_code, 403)

        wrong_tenant_claim = {
            "sub": str(self.owner.id),
            "tenant_id": self.other_tenant.id,
            "role": "owner",
        }
        with self.assertRaises(HTTPException) as tenant_error:
            require_document_delete_access(
                current_user=wrong_tenant_claim, db=self.db
            )
        self.assertEqual(tenant_error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
