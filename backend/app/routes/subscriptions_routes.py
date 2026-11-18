from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Plan, Subscription
from backend.app.schemas.subscriptions import (
    PlanResponse,
    SubscriptionActivationRequest,
    SubscriptionCancelResponse,
    SubscriptionPlanUpdate,
    SubscriptionResponse,
    SubscriptionUsageResponse,
)
from backend.app.services.usage import get_tenant_usage
from backend.app.services.subscriptions import (
    activate_subscription,
    cancel_subscription as cancel_subscription_lifecycle,
    change_subscription_plan,
    renew_subscription,
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


@router.get(
    "/subscription/usage",
    response_model=SubscriptionUsageResponse,
)
def get_subscription_usage(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_TENANT_ROLES)),
):
    return get_tenant_usage(db, int(current_user["tenant_id"]))


@router.post("/subscription/activate", response_model=SubscriptionResponse)
def activate_tenant_subscription(
    activation: SubscriptionActivationRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_MANAGEMENT_ROLES)),
):
    return activate_subscription(
        db, int(current_user["tenant_id"]), activation.plan_code
    )


@router.patch("/subscription", response_model=SubscriptionResponse)
def update_subscription_plan(
    update: SubscriptionPlanUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_MANAGEMENT_ROLES)),
):
    return change_subscription_plan(
        db, int(current_user["tenant_id"]), update.plan_code
    )


@router.post(
    "/subscription/cancel",
    response_model=SubscriptionCancelResponse,
)
def cancel_subscription(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_MANAGEMENT_ROLES)),
):
    return {
        "subscription": cancel_subscription_lifecycle(
            db, int(current_user["tenant_id"])
        )
    }


@router.post("/subscription/renew", response_model=SubscriptionResponse)
def renew_tenant_subscription(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_MANAGEMENT_ROLES)),
):
    return renew_subscription(db, int(current_user["tenant_id"]))
