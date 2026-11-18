from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from backend.app.models import Plan, Subscription

SUBSCRIPTION_PERIOD_DAYS = 30


def _get_subscription(db: Session, tenant_id: int) -> Subscription | None:
    return (
        db.query(Subscription)
        .filter(Subscription.tenant_id == tenant_id)
        .one_or_none()
    )


def _get_active_plan(db: Session, plan_code: str) -> Plan:
    plan = (
        db.query(Plan)
        .filter(Plan.code == plan_code, Plan.is_active.is_(True))
        .one_or_none()
    )
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or inactive plan",
        )
    return plan


def _period_end(start: datetime) -> datetime:
    return start + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def activate_subscription(
    db: Session, tenant_id: int, plan_code: str
) -> Subscription:
    plan = _get_active_plan(db, plan_code)
    subscription = _get_subscription(db, tenant_id)
    now = datetime.now(timezone.utc)

    if (
        subscription is not None
        and subscription.status == "active"
        and subscription.plan_id == plan.id
        and _as_utc(subscription.current_period_end) > now
        and not subscription.cancel_at_period_end
    ):
        return subscription

    if subscription is None:
        subscription = Subscription(tenant_id=tenant_id, plan_id=plan.id)
        db.add(subscription)

    subscription.plan_id = plan.id
    subscription.status = "active"
    subscription.current_period_start = now
    subscription.current_period_end = _period_end(now)
    subscription.trial_end = None
    subscription.cancel_at_period_end = False
    subscription.canceled_at = None
    db.commit()
    db.refresh(subscription)
    return subscription


def change_subscription_plan(
    db: Session, tenant_id: int, plan_code: str
) -> Subscription:
    subscription = _get_subscription(db, tenant_id)
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )
    if subscription.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active subscriptions can change plans",
        )

    plan = _get_active_plan(db, plan_code)
    if subscription.plan_id == plan.id and not subscription.cancel_at_period_end:
        return subscription

    subscription.plan_id = plan.id
    subscription.cancel_at_period_end = False
    db.commit()
    db.refresh(subscription)
    return subscription


def cancel_subscription(db: Session, tenant_id: int) -> Subscription:
    subscription = _get_subscription(db, tenant_id)
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )
    if subscription.status == "canceled":
        return subscription
    if subscription.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active subscriptions can be canceled",
        )
    if subscription.cancel_at_period_end:
        return subscription

    subscription.cancel_at_period_end = True
    db.commit()
    db.refresh(subscription)
    return subscription


def renew_subscription(db: Session, tenant_id: int) -> Subscription:
    subscription = _get_subscription(db, tenant_id)
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )
    if not subscription.plan.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The subscription plan is inactive",
        )

    now = datetime.now(timezone.utc)
    period_end = _as_utc(subscription.current_period_end)

    if subscription.status == "active" and period_end > now:
        if not subscription.cancel_at_period_end:
            return subscription

        subscription.cancel_at_period_end = False
        db.commit()
        db.refresh(subscription)
        return subscription

    start = period_end if period_end > now else now
    subscription.status = "active"
    subscription.current_period_start = start
    subscription.current_period_end = _period_end(start)
    subscription.cancel_at_period_end = False
    subscription.canceled_at = None
    db.commit()
    db.refresh(subscription)
    return subscription


def expire_subscriptions(db: Session, now: datetime | None = None) -> int:
    from sqlalchemy import update

    expiry_time = now or datetime.now(timezone.utc)
    result = db.execute(
        update(Subscription)
        .where(
            Subscription.status.in_(("active", "trialing")),
            Subscription.current_period_end <= expiry_time,
        )
        .values(
            status="canceled",
            canceled_at=expiry_time,
            cancel_at_period_end=False,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount
