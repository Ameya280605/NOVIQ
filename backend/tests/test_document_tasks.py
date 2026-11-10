import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "document-task-test-signing-key"
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.celery_app import celery_app
from backend.app.models import AuditLog, Base, Document, Project, Tenant, User
from backend.app.services.s3_storage import StorageServiceError
from backend.app.tasks import documents as document_tasks


class DocumentCleanupTaskTests(unittest.TestCase):
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
            tenant = Tenant(name="Document task tenant")
            foreign_tenant = Tenant(name="Foreign document task tenant")
            db.add_all([tenant, foreign_tenant])
            db.flush()
            self.owner = User(
                tenant_id=tenant.id,
                name="Owner",
                email="document-task-owner@example.test",
                password_hash="test-hash",
                role="owner",
            )
            self.foreign_owner = User(
                tenant_id=foreign_tenant.id,
                name="Foreign owner",
                email="document-task-foreign@example.test",
                password_hash="test-hash",
                role="owner",
            )
            self.project = Project(tenant_id=tenant.id, name="Project")
            self.foreign_project = Project(
                tenant_id=foreign_tenant.id, name="Foreign project"
            )
            db.add_all(
                [self.owner, self.foreign_owner, self.project, self.foreign_project]
            )
            db.flush()
            self.document = self._new_document(
                tenant_id=tenant.id,
                project_id=self.project.id,
                uploaded_by=self.owner.id,
                deletion_requested_by=self.owner.id,
            )
            self.foreign_document = self._new_document(
                tenant_id=foreign_tenant.id,
                project_id=self.foreign_project.id,
                uploaded_by=self.foreign_owner.id,
                deletion_requested_by=self.foreign_owner.id,
                storage_key="documents/" + "b" * 32,
            )
            db.add_all([self.document, self.foreign_document])
            db.commit()
            self.tenant_id = tenant.id
            self.foreign_tenant_id = foreign_tenant.id
            self.owner_id = self.owner.id
            self.project_id = self.project.id
            self.document_id = self.document.id
            self.foreign_document_id = self.foreign_document.id

        self.eager_settings = (
            celery_app.conf.task_always_eager,
            celery_app.conf.task_eager_propagates,
        )
        celery_app.conf.update(
            task_always_eager=True,
            task_eager_propagates=False,
        )
        self.session_patch = patch.object(
            document_tasks, "SessionLocal", self.session_factory
        )
        self.session_patch.start()

    def tearDown(self) -> None:
        celery_app.conf.update(
            task_always_eager=self.eager_settings[0],
            task_eager_propagates=self.eager_settings[1],
        )
        self.session_patch.stop()
        self.engine.dispose()

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        del _record
        connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _new_document(**overrides) -> Document:
        values = {
            "tenant_id": 1,
            "project_id": 1,
            "uploaded_by": 1,
            "original_filename": "doc.pdf",
            "storage_key": "documents/" + "a" * 32,
            "content_type": "application/pdf",
            "size_bytes": 100,
            "status": "deletion_pending",
            "deleted_at": datetime.now(timezone.utc),
        }
        values.update(overrides)
        return Document(**values)

    def _read_document(self, document_id: int) -> Document:
        with self.session_factory() as db:
            return db.get(Document, document_id)

    def test_successful_cleanup_marks_deleted_and_audits_once(self) -> None:
        with patch.object(
            document_tasks.s3_storage, "delete_object"
        ) as delete_object:
            result = document_tasks.cleanup_document_object.apply(
                args=(self.tenant_id, self.document_id, False), throw=False
            )
            duplicate = document_tasks.cleanup_document_object.apply(
                args=(self.tenant_id, self.document_id, False), throw=False
            )

        self.assertTrue(result.successful(), repr(result.result))
        self.assertEqual(result.result["status"], "deleted")
        self.assertTrue(duplicate.successful(), repr(duplicate.result))
        self.assertEqual(duplicate.result["status"], "already_deleted")
        delete_object.assert_called_once_with("documents/" + "a" * 32)
        self.assertEqual(self._read_document(self.document_id).status, "deleted")
        with self.session_factory() as db:
            events = (
                db.query(AuditLog)
                .filter(
                    AuditLog.action == "document_deleted",
                    AuditLog.resource_id == self.document_id,
                )
                .all()
            )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].tenant_id, self.tenant_id)
        self.assertEqual(events[0].user_id, self.owner_id)

    def test_transient_s3_failure_retries_with_backoff(self) -> None:
        transient = StorageServiceError("temporary", retryable=True)
        with patch.object(
            document_tasks.s3_storage,
            "delete_object",
            side_effect=[transient, None],
        ) as delete_object:
            result = document_tasks.cleanup_document_object.apply(
                args=(self.tenant_id, self.document_id, False), throw=False
            )

        self.assertTrue(result.successful(), repr(result.result))
        self.assertEqual(result.result["status"], "deleted")
        self.assertEqual(delete_object.call_count, 2)
        self.assertEqual(self._read_document(self.document_id).status, "deleted")

    def test_s3_retries_are_bounded_and_leave_document_recoverable(self) -> None:
        transient_failures = [
            StorageServiceError("temporary", retryable=True)
            for _ in range(document_tasks.MAX_RETRIES + 1)
        ]
        with patch.object(
            document_tasks.s3_storage,
            "delete_object",
            side_effect=transient_failures,
        ) as delete_object:
            result = document_tasks.cleanup_document_object.apply(
                args=(self.tenant_id, self.document_id, False), throw=False
            )

        self.assertTrue(result.failed())
        self.assertEqual(delete_object.call_count, document_tasks.MAX_RETRIES + 1)
        self.assertEqual(
            self._read_document(self.document_id).status, "deletion_pending"
        )

    def test_permanent_failure_keeps_cleanup_recoverable(self) -> None:
        with patch.object(
            document_tasks.s3_storage,
            "delete_object",
            side_effect=StorageServiceError("permanent", retryable=False),
        ):
            result = document_tasks.cleanup_document_object.apply(
                args=(self.tenant_id, self.document_id, False), throw=False
            )
        self.assertTrue(result.failed())
        self.assertEqual(
            self._read_document(self.document_id).status, "deletion_pending"
        )

        with patch.object(document_tasks.s3_storage, "delete_object"):
            recovered = document_tasks.cleanup_document_object.apply(
                args=(self.tenant_id, self.document_id, False), throw=False
            )
        self.assertTrue(recovered.successful(), repr(recovered.result))
        self.assertEqual(self._read_document(self.document_id).status, "deleted")

    def test_cross_tenant_job_cannot_access_or_delete_document(self) -> None:
        with patch.object(
            document_tasks.s3_storage, "delete_object"
        ) as delete_object:
            result = document_tasks.cleanup_document_object.apply(
                args=(
                    self.tenant_id,
                    self.foreign_document_id,
                    False,
                ),
                throw=False,
            )
        self.assertTrue(result.successful(), repr(result.result))
        self.assertEqual(result.result["status"], "not_found")
        delete_object.assert_not_called()
        self.assertEqual(
            self._read_document(self.foreign_document_id).status,
            "deletion_pending",
        )

    def test_abandoned_pending_upload_is_cleaned_only_after_ttl(self) -> None:
        old_upload = Document(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            uploaded_by=self.owner_id,
            original_filename="old.pdf",
            storage_key="documents/" + "c" * 32,
            content_type="application/pdf",
            size_bytes=100,
            status="pending_upload",
            created_at=datetime.now(timezone.utc)
            - timedelta(seconds=document_tasks.PENDING_UPLOAD_TTL_SECONDS + 1),
            updated_at=datetime.now(timezone.utc),
        )
        recent_upload = Document(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            uploaded_by=self.owner_id,
            original_filename="recent.pdf",
            storage_key="documents/" + "d" * 32,
            content_type="application/pdf",
            size_bytes=100,
            status="pending_upload",
        )
        with self.session_factory() as db:
            db.add_all([old_upload, recent_upload])
            db.commit()
            old_id = old_upload.id
            recent_id = recent_upload.id

        with patch.object(
            document_tasks.s3_storage, "delete_object"
        ) as delete_object:
            old_result = document_tasks.cleanup_document_object.apply(
                args=(self.tenant_id, old_id, True), throw=False
            )
            recent_result = document_tasks.cleanup_document_object.apply(
                args=(self.tenant_id, recent_id, True), throw=False
            )
        self.assertTrue(old_result.successful(), repr(old_result.result))
        self.assertEqual(old_result.result["status"], "deleted")
        self.assertEqual(self._read_document(old_id).status, "deleted")
        self.assertEqual(recent_result.result["status"], "not_eligible")
        self.assertEqual(delete_object.call_count, 1)

    def test_recovery_scan_queues_stale_tenant_scoped_records(self) -> None:
        stale_pending = Document(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            uploaded_by=self.owner_id,
            original_filename="stale.pdf",
            storage_key="documents/" + "e" * 32,
            content_type="application/pdf",
            size_bytes=100,
            status="pending_upload",
            created_at=datetime.now(timezone.utc)
            - timedelta(seconds=document_tasks.PENDING_UPLOAD_TTL_SECONDS + 1),
            updated_at=datetime.now(timezone.utc),
        )
        stale_deletion = Document(
            tenant_id=self.tenant_id,
            project_id=self.document.project_id,
            uploaded_by=self.owner_id,
            deletion_requested_by=self.owner_id,
            original_filename="deleting.pdf",
            storage_key="documents/" + "f" * 32,
            content_type="application/pdf",
            size_bytes=100,
            status="deletion_pending",
            deleted_at=datetime.now(timezone.utc)
            - timedelta(seconds=document_tasks.CLEANUP_RECOVERY_GRACE_SECONDS + 1),
            updated_at=datetime.now(timezone.utc)
            - timedelta(seconds=document_tasks.CLEANUP_RECOVERY_GRACE_SECONDS + 1),
        )
        with self.session_factory() as db:
            db.add_all([stale_pending, stale_deletion])
            db.commit()
            stale_pending_id = stale_pending.id
            stale_deletion_id = stale_deletion.id

        with patch.object(
            document_tasks,
            "enqueue_document_cleanup",
            return_value=True,
        ) as enqueue:
            result = document_tasks.recover_document_cleanups.apply(throw=False)
        self.assertTrue(result.successful(), repr(result.result))
        self.assertEqual(result.result["queued"], 2)
        self.assertEqual(enqueue.call_count, 2)
        queued = {
            (call.args[0], call.args[1], call.kwargs["recover_pending_upload"])
            for call in enqueue.call_args_list
        }
        self.assertIn(
            (self.tenant_id, stale_pending_id, True),
            queued,
        )
        self.assertIn(
            (self.tenant_id, stale_deletion_id, False),
            queued,
        )


if __name__ == "__main__":
    unittest.main()
