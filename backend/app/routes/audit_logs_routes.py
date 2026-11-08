from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import AuditLog
from backend.app.schemas.audit_logs import AuditLogListResponse
from backend.app.utils.roles import require_roles


router = APIRouter(prefix="/audit-logs", tags=["Audit Logs"])


@router.get("", response_model=AuditLogListResponse)
def get_audit_logs(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles("owner", "admin")),
    action: str | None = None,
    resource_type: str | None = None,
    user_id: int | None = Query(default=None, ge=1),
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    if (
        (start_date is not None and start_date.utcoffset() is None)
        or (end_date is not None and end_date.utcoffset() is None)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date filters must include a UTC offset",
        )
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must be earlier than or equal to end_date",
        )

    query = db.query(AuditLog).filter(
        AuditLog.tenant_id == current_user["tenant_id"]
    )
    if action is not None:
        query = query.filter(AuditLog.action == action)
    if resource_type is not None:
        query = query.filter(AuditLog.resource_type == resource_type)
    if user_id is not None:
        query = query.filter(AuditLog.user_id == user_id)
    if start_date is not None:
        query = query.filter(
            AuditLog.created_at >= start_date.astimezone(timezone.utc)
        )
    if end_date is not None:
        query = query.filter(AuditLog.created_at <= end_date.astimezone(timezone.utc))

    total = query.count()
    total_pages = (total + page_size - 1) // page_size
    items = (
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }