import re
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import AuditLog

_SENSITIVE_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|credential|cookie)",
    re.IGNORECASE,
)
_ALLOWED_METADATA_KEYS = {
    "role",
    "old_role",
    "new_role",
    "changed_fields",
    "project_id",
    "assigned_to",
    "old_assigned_to",
    "new_assigned_to",
    "old_status",
    "new_status",
    "status",
    "project_manager_id",
    "old_project_manager_id",
    "new_project_manager_id",
    "document_id",
    "content_type",
    "size_bytes",
    "document_status",
    "count",
}


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_metadata(item)
            for key, item in value.items()
            if str(key) in _ALLOWED_METADATA_KEYS
            and not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_metadata(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return None


def create_audit_log(
    db: Session,
    tenant_id: int,
    user_id: int,
    action: str,
    resource_type: str,
    resource_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    audit_log = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata_json=_sanitize_metadata(metadata) if metadata is not None else None,
    )

    db.add(audit_log)