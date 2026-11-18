from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Plan, Subscription
from backend.app.schemas.subscriptions import (
    PlanResponse,
    SubscriptionCancelResponse,
    SubscriptionPlanUpdate,
    SubscriptionResponse,
)
from backend.app.utils.roles import require_roles

router = APIRouter(tags=["Subscriptions"])
_TENANT_ROLES = ("owner", "admin", "manager", "member")
_MANAGEMENT_ROLES = ("owner", "admin")


@router.get("/plans", response_model=list[PlanResponse])
def list_active_plans(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_TENANT_ROLES)),
):
    del current_user
    return (
        db.query(Plan)
        .filter(Plan.is_active.is_(True))
        .order_by(Plan.id)
        .all()
    )


def _get_tenant_subscription(db: Session, tenant_id: int) -> Subscription:
    subscription = (
        db.query(Subscription)
        .filter(Subscription.tenant_id == tenant_id)
        .one_or_none()
    )
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )
    return subscription


@router.get("/subscription", response_model=SubscriptionResponse)
def get_subscription(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_TENANT_ROLES)),
):
    return _get_tenant_subscription(db, int(current_user["tenant_id"]))


@router.patch("/subscription", response_model=SubscriptionResponse)
def change_subscription_plan(
    update: SubscriptionPlanUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_MANAGEMENT_ROLES)),
):
    subscription = _get_tenant_subscription(db, int(current_user["tenant_id"]))
    if subscription.status == "canceled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Canceled subscriptions cannot change plans",
        )

    plan = (
        db.query(Plan)
        .filter(Plan.code == update.plan_code, Plan.is_active.is_(True))
        .one_or_none()
    )
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or inactive plan",
        )

    subscription.plan_id = plan.id
    subscription.cancel_at_period_end = False
    db.commit()
    db.refresh(subscription)
    return subscription


@router.post(
    "/subscription/cancel",
    response_model=SubscriptionCancelResponse,
)
def cancel_subscription(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_MANAGEMENT_ROLES)),
):
    subscription = _get_tenant_subscription(db, int(current_user["tenant_id"]))
    if subscription.status != "canceled":
        subscription.cancel_at_period_end = True
        db.commit()
        db.refresh(subscription)
    return {"subscription": subscription}
