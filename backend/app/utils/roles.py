from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import User
from backend.app.utils.jwt import get_current_user


def require_roles(*allowed_roles):
    def role_checker(
        current_user: dict = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        try:
            user_id = int(current_user["sub"])
            tenant_id = int(current_user["tenant_id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )

        user = db.query(User).filter(User.id == user_id).first()
        if user is None or user.tenant_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )

        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action"
            )

        verified_user = dict(current_user)
        verified_user["sub"] = str(user.id)
        verified_user["tenant_id"] = user.tenant_id
        verified_user["role"] = user.role
        return verified_user

    return role_checker