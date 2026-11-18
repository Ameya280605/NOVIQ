from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import PlanEntitlement, Subscription
from backend.app.utils.roles import require_roles


def require_entitlement(feature_key: str, *allowed_roles: str):
    role_checker = require_roles(*allowed_roles)

    def entitlement_checker(
        current_user: dict = Depends(role_checker),
        db: Session = Depends(get_db),
    ) -> dict:
        subscription = (
            db.query(Subscription)
            .join(Subscription.plan)
            .filter(
                Subscription.tenant_id == int(current_user["tenant_id"]),
                Subscription.status == "active",
            )
            .one_or_none()
        )
        if subscription is None or not subscription.plan.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="An active subscription is required for this feature",
            )

        entitlement = (
            db.query(PlanEntitlement)
            .filter(
                PlanEntitlement.plan_id == subscription.plan_id,
                PlanEntitlement.feature_key == feature_key,
                PlanEntitlement.enabled.is_(True),
            )
            .one_or_none()
        )
        if entitlement is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Feature '{feature_key}' is not enabled for this plan",
            )
        return current_user

    return entitlement_checker
