import logging
import math
import re
from datetime import datetime, timezone
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Document, Project
from backend.app.schemas.documents import (
    DocumentDownloadResponse,
    DocumentDeletionResponse,
    DocumentListResponse,
    DocumentMetadata,
    DocumentUploadRequest,
    DocumentUploadResponse,
)
from backend.app.services.s3_storage import (
    ObjectMetadataMismatchError,
    ObjectNotFoundError,
    StorageConfigurationError,
    StorageServiceError,
    s3_storage,
)
from backend.app.services.document_jobs import enqueue_document_cleanup
from backend.app.utils.audit import create_audit_log
from backend.app.utils.documents import (
    get_tenant_document,
    get_tenant_project,
    require_document_delete_access,
    require_document_read_access,
    require_document_write_access,
)

router = APIRouter(prefix="/projects/{project_id}/documents", tags=["Documents"])
logger = logging.getLogger(__name__)
_CHECKSUM_PATTERN = re.compile(r"[A-Za-z0-9+/=_-]{1,128}\Z")


def _record_document_audit(
    db: Session,
    tenant_id: int,
    user_id: int,
    action: str,
    document: Document,
    *,
    count: int | None = None,
) -> None:
    metadata = {
        "document_id": document.id,
        "project_id": document.project_id,
        "document_status": document.status,
        "content_type": document.content_type,
        "size_bytes": document.size_bytes,
        "original_filename": document.original_filename,
    }
    if count is not None:
        metadata["count"] = count
    create_audit_log(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        resource_type="document",
        resource_id=document.id,
        metadata=metadata,
    )


def _raise_database_error(
    db: Session,
    operation: str,
    tenant_id: int,
    project_id: int,
    exc: SQLAlchemyError,
) -> NoReturn:
    db.rollback()
    logger.error(
        "document_database_operation_failed",
        extra={
            "event_name": "document_database_operation_failed",
            "operation": operation,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "error_type": type(exc).__name__,
        },
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Document service is temporarily unavailable",
    )


def _get_tenant_project(db: Session, project_id: int, tenant_id: int) -> Project:
    try:
        return get_tenant_project(db, project_id, tenant_id)
    except SQLAlchemyError as exc:
        _raise_database_error(db, "get_project", tenant_id, project_id, exc)


def _get_tenant_document(
    db: Session,
    document_id: int,
    tenant_id: int,
    project_id: int,
    *,
    include_deleted: bool = False,
) -> Document:
    try:
        return get_tenant_document(
            db,
            document_id,
            tenant_id,
            project_id=project_id,
            include_deleted=include_deleted,
        )
    except SQLAlchemyError as exc:
        _raise_database_error(db, "get_document", tenant_id, project_id, exc)


@router.post("", response_model=DocumentUploadResponse)
def initiate_document_upload(
    project_id: int,
    upload: DocumentUploadRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_document_write_access),
):
    tenant_id = current_user["tenant_id"]
    user_id = int(current_user["sub"])
    _get_tenant_project(db, project_id, tenant_id)

    try:
        signed_upload = s3_storage.create_upload_url(upload.content_type)
    except StorageConfigurationError as exc:
        logger.error(
            "document_storage_not_configured",
            extra={"event_name": "document_storage_not_configured"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is not configured",
        ) from exc
    except StorageServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is temporarily unavailable",
        ) from exc

    document = Document(
        tenant_id=tenant_id,
        project_id=project_id,
        uploaded_by=user_id,
        original_filename=upload.original_filename,
        storage_key=signed_upload["object_key"],
        content_type=upload.content_type,
        size_bytes=upload.size_bytes,
        status="pending_upload",
    )
    try:
        db.add(document)
        db.flush()
        _record_document_audit(
            db,
            tenant_id,
            user_id,
            "document_upload_initiated",
            document,
        )
        db.commit()
        db.refresh(document)
    except SQLAlchemyError as exc:
        _raise_database_error(
            db, "initiate_upload", tenant_id, project_id, exc
        )

    return {
        "document": document,
        "upload_url": signed_upload["url"],
        "headers": signed_upload["headers"],
        "expires_in": s3_storage.presigned_url_ttl_seconds,
    }


@router.post(
    "/{document_id}/complete",
    response_model=DocumentMetadata,
)
def complete_document_upload(
    project_id: int,
    document_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_document_write_access),
):
    tenant_id = current_user["tenant_id"]
    user_id = int(current_user["sub"])
    _get_tenant_project(db, project_id, tenant_id)
    document = _get_tenant_document(db, document_id, tenant_id, project_id)
    if document.status == "available":
        return document
    if document.status != "pending_upload":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is not awaiting upload completion",
        )

    try:
        stored_metadata = s3_storage.verify_uploaded_object(
            document.storage_key,
            document.content_type,
            document.size_bytes,
        )
    except ObjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Uploaded object is not present yet",
        ) from exc
    except ObjectMetadataMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Uploaded object does not match the initiated upload",
        ) from exc
    except StorageConfigurationError as exc:
        logger.error(
            "document_storage_not_configured",
            extra={"event_name": "document_storage_not_configured"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is not configured",
        ) from exc
    except StorageServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is temporarily unavailable",
        ) from exc

    if stored_metadata.checksum_sha256 is not None:
        if not _CHECKSUM_PATTERN.fullmatch(stored_metadata.checksum_sha256):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Stored object checksum metadata is invalid",
            )
        document.checksum = stored_metadata.checksum_sha256
    document.status = "available"
    try:
        _record_document_audit(
            db,
            tenant_id,
            user_id,
            "document_upload_completed",
            document,
        )
        db.commit()
        db.refresh(document)
    except SQLAlchemyError as exc:
        _raise_database_error(db, "complete_upload", tenant_id, project_id, exc)
    return document


@router.get("", response_model=DocumentListResponse)
def list_project_documents(
    project_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_document_read_access),
):
    tenant_id = current_user["tenant_id"]
    user_id = int(current_user["sub"])
    _get_tenant_project(db, project_id, tenant_id)
    query = db.query(Document).filter(
        Document.tenant_id == tenant_id,
        Document.project_id == project_id,
        Document.deleted_at.is_(None),
    )
    try:
        total = query.count()
        items = (
            query.order_by(Document.created_at.desc(), Document.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        create_audit_log(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            action="documents_listed",
            resource_type="project",
            resource_id=project_id,
            metadata={"project_id": project_id, "count": total},
        )
        db.commit()
    except SQLAlchemyError as exc:
        _raise_database_error(db, "list_documents", tenant_id, project_id, exc)

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": math.ceil(total / page_size) if total else 0,
    }


@router.get("/{document_id}", response_model=DocumentMetadata)
def get_document_metadata(
    project_id: int,
    document_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_document_read_access),
):
    tenant_id = current_user["tenant_id"]
    user_id = int(current_user["sub"])
    _get_tenant_project(db, project_id, tenant_id)
    document = _get_tenant_document(db, document_id, tenant_id, project_id)
    try:
        _record_document_audit(
            db, tenant_id, user_id, "document_viewed", document
        )
        db.commit()
    except SQLAlchemyError as exc:
        _raise_database_error(db, "get_document", tenant_id, project_id, exc)
    return document


@router.post(
    "/{document_id}/download-url",
    response_model=DocumentDownloadResponse,
)
def create_document_download_url(
    project_id: int,
    document_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_document_read_access),
):
    tenant_id = current_user["tenant_id"]
    user_id = int(current_user["sub"])
    _get_tenant_project(db, project_id, tenant_id)
    document = _get_tenant_document(db, document_id, tenant_id, project_id)
    if document.status != "available":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is not available for download",
        )
    try:
        s3_storage.get_object_metadata(document.storage_key)
        download_url = s3_storage.create_download_url(document.storage_key)
    except ObjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document object was not found",
        ) from exc
    except StorageConfigurationError as exc:
        logger.error(
            "document_storage_not_configured",
            extra={"event_name": "document_storage_not_configured"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is not configured",
        ) from exc
    except StorageServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is temporarily unavailable",
        ) from exc

    try:
        _record_document_audit(
            db,
            tenant_id,
            user_id,
            "document_download_url_created",
            document,
        )
        db.commit()
    except SQLAlchemyError as exc:
        _raise_database_error(
            db, "create_download_url", tenant_id, project_id, exc
        )
    return {
        "download_url": download_url,
        "expires_in": s3_storage.presigned_url_ttl_seconds,
    }


@router.delete(
    "/{document_id}",
    response_model=DocumentDeletionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_document(
    project_id: int,
    document_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_document_delete_access),
):
    tenant_id = current_user["tenant_id"]
    user_id = int(current_user["sub"])
    _get_tenant_project(db, project_id, tenant_id)
    document = _get_tenant_document(
        db, document_id, tenant_id, project_id, include_deleted=True
    )
    if document.status == "deleted":
        return {
            "document_id": document.id,
            "status": "deleted",
            "cleanup_queued": False,
        }
    if document.status == "deletion_pending" or document.deleted_at is not None:
        return {
            "document_id": document.id,
            "status": "deletion_pending",
            "cleanup_queued": False,
        }

    try:
        transition = db.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.project_id == project_id,
                Document.tenant_id == tenant_id,
                Document.deleted_at.is_(None),
                Document.status.notin_(("deletion_pending", "deleted")),
            )
            .values(
                status="deletion_pending",
                deleted_at=datetime.now(timezone.utc),
                deletion_requested_by=user_id,
            )
        )
        if transition.rowcount != 1:
            db.rollback()
            document = _get_tenant_document(
                db, document_id, tenant_id, project_id, include_deleted=True
            )
            completed = document.status == "deleted"
            return {
                "document_id": document.id,
                "status": "deleted" if completed else "deletion_pending",
                "cleanup_queued": False,
            }
        db.expire(document)
        db.refresh(document)
        _record_document_audit(
            db,
            tenant_id,
            user_id,
            "document_deletion_requested",
            document,
        )
        db.commit()
    except SQLAlchemyError as exc:
        _raise_database_error(
            db, "request_deletion", tenant_id, project_id, exc
        )

    cleanup_queued = enqueue_document_cleanup(tenant_id, document.id)
    return {
        "document_id": document.id,
        "status": "deletion_pending",
        "cleanup_queued": cleanup_queued,
    }
