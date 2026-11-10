from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from backend.app.models import Document, Project
from backend.app.utils.roles import require_roles

DOCUMENT_READ_ROLES = ("owner", "admin", "manager", "member")
DOCUMENT_WRITE_ROLES = ("owner", "admin", "manager")
DOCUMENT_DELETE_ROLES = ("owner", "admin")

require_document_read_access = require_roles(*DOCUMENT_READ_ROLES)
require_document_write_access = require_roles(*DOCUMENT_WRITE_ROLES)
require_document_delete_access = require_roles(*DOCUMENT_DELETE_ROLES)


def get_tenant_project(db: Session, project_id: int, tenant_id: int) -> Project:
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.tenant_id == tenant_id)
        .first()
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return project


def get_tenant_document(
    db: Session,
    document_id: int,
    tenant_id: int,
    project_id: int | None = None,
    include_deleted: bool = False,
) -> Document:
    query = db.query(Document).filter(
        Document.id == document_id,
        Document.tenant_id == tenant_id,
    )
    if not include_deleted:
        query = query.filter(Document.deleted_at.is_(None))
    if project_id is not None:
        query = query.filter(Document.project_id == project_id)

    document = query.first()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )
    return document
