from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import AuditLog
from backend.app.schemas.audit_logs import AuditLogResponse
from backend.app.utils.jwt import get_current_user


router = APIRouter(
    prefix="/audit-logs",
    tags=["Audit Logs"]
)


@router.get("", response_model=list[AuditLogResponse])
def get_audit_logs(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    audit_logs = (
        db.query(AuditLog)
        .filter(
            AuditLog.tenant_id == current_user["tenant_id"]
        )
        .order_by(AuditLog.created_at.desc())
        .all()
    )

    return audit_logs