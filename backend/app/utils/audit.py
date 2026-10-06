from sqlalchemy.orm import Session

from backend.app.models import AuditLog


def create_audit_log(
    db: Session,
    tenant_id: int,
    user_id: int,
    action: str,
    resource_type: str,
    resource_id: int | None = None,
    details: str | None = None,
):
    audit_log = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
    )

    db.add(audit_log)