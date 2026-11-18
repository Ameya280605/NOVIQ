from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.models import Document, Project, Subscription, Task, User

UsageResource = Literal["users", "projects", "tasks", "documents", "storage_bytes"]

_PLAN_LIMIT_FIELDS: dict[UsageResource, str] = {
    "users": "max_users",
    "projects": "max_projects",
    "tasks": "max_tasks",
    "documents": "max_documents",
    "storage_bytes": "max_storage_bytes",
}


def _get_subscription(db: Session, tenant_id: int) -> Subscription | None:
    return (
        db.query(Subscription)
        .filter(Subscription.tenant_id == tenant_id)
        .one_or_none()
    )


def _get_active_subscription(db: Session, tenant_id: int) -> Subscription:
    subscription = _get_subscription(db, tenant_id)
    if (
        subscription is None
        or subscription.status != "active"
        or not subscription.plan.is_active
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An active subscription is required for quota enforcement",
        )
    return subscription


def _usage_value(db: Session, tenant_id: int, resource: UsageResource) -> int:
    if resource == "users":
        return db.query(func.count(User.id)).filter(
            User.tenant_id == tenant_id
        ).scalar() or 0
    if resource == "projects":
        return db.query(func.count(Project.id)).filter(
            Project.tenant_id == tenant_id
        ).scalar() or 0
    if resource == "tasks":
        return (
            db.query(func.count(Task.id))
            .join(Project, Task.project_id == Project.id)
            .filter(Project.tenant_id == tenant_id)
            .scalar() or 0
        )
    if resource == "documents":
        return db.query(func.count(Document.id)).filter(
            Document.tenant_id == tenant_id,
            Document.deleted_at.is_(None),
        ).scalar() or 0
    return (
        db.query(func.coalesce(func.sum(Document.size_bytes), 0))
        .filter(
            Document.tenant_id == tenant_id,
            Document.deleted_at.is_(None),
        )
        .scalar() or 0
    )


def get_tenant_usage(db: Session, tenant_id: int) -> dict[str, dict[str, int | None]]:
    subscription = _get_active_subscription(db, tenant_id)
    plan = subscription.plan
    return {
        resource: {
            "used": _usage_value(db, tenant_id, resource),
            "limit": getattr(plan, _PLAN_LIMIT_FIELDS[resource]),
        }
        for resource in _PLAN_LIMIT_FIELDS
    }


def enforce_quota(
    db: Session,
    tenant_id: int,
    resource: UsageResource,
    additional: int = 1,
) -> None:
    if additional < 0:
        raise ValueError("additional usage must not be negative")

    subscription = _get_active_subscription(db, tenant_id)
    limit = getattr(subscription.plan, _PLAN_LIMIT_FIELDS[resource])
    if limit is None:
        return

    used = _usage_value(db, tenant_id, resource)
    if used + additional > limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{resource} quota exceeded: limit {limit}, "
                f"current usage {used}, requested {additional}"
            ),
        )
