from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Notification
from backend.app.schemas.notifications import (
    NotificationListResponse,
    NotificationReadAllResponse,
    NotificationResponse,
    UnreadCountResponse,
)
from backend.app.utils.roles import require_roles

router = APIRouter(prefix="/notifications", tags=["Notifications"])
_NOTIFICATION_ROLES = ("owner", "admin", "manager", "member")


@router.get("", response_model=NotificationListResponse)
def get_notifications(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_NOTIFICATION_ROLES)),
):
    query = db.query(Notification).filter(
        Notification.tenant_id == current_user["tenant_id"],
        Notification.recipient_id == int(current_user["sub"]),
    )
    total = query.count()
    items = (
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/unread-count", response_model=UnreadCountResponse)
def get_unread_count(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_NOTIFICATION_ROLES)),
):
    count = db.query(Notification).filter(
        Notification.tenant_id == current_user["tenant_id"],
        Notification.recipient_id == int(current_user["sub"]),
        Notification.is_read.is_(False),
    ).count()
    return {"unread_count": count}


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_NOTIFICATION_ROLES)),
):
    notification = db.query(Notification).filter(
        Notification.id == notification_id,
        Notification.tenant_id == current_user["tenant_id"],
        Notification.recipient_id == int(current_user["sub"]),
    ).one_or_none()
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.is_read = True
    db.commit()
    db.refresh(notification)
    return notification


@router.patch("/read-all", response_model=NotificationReadAllResponse)
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_NOTIFICATION_ROLES)),
):
    result = db.execute(
        update(Notification)
        .where(
            Notification.tenant_id == current_user["tenant_id"],
            Notification.recipient_id == int(current_user["sub"]),
            Notification.is_read.is_(False),
        )
        .values(is_read=True)
    )
    db.commit()
    return {"updated_count": result.rowcount}
