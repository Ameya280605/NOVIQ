import os
from datetime import datetime, timedelta, timezone

from jose import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
MIN_SECRET_KEY_LENGTH = 32
MIN_UNIQUE_SECRET_CHARACTERS = 16


def validate_jwt_secret() -> None:
    """Require a securely generated, high-entropy signing secret."""
    if (
        not SECRET_KEY
        or len(SECRET_KEY) < MIN_SECRET_KEY_LENGTH
        or len(set(SECRET_KEY)) < MIN_UNIQUE_SECRET_CHARACTERS
    ):
        raise RuntimeError(
            "JWT_SECRET_KEY must be a high-entropy value of at least 32 characters"
        )


def create_access_token(data: dict):
    to_encode = data.copy()

    expire = datetime.now(timezone.utc) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )

    to_encode.update({
        "exp": expire
    })

    return jwt.encode(
        to_encode,
        SECRET_KEY,
        algorithm=ALGORITHM
    )

security = HTTPBearer()
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    try:
        token = credentials.credentials

        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        return payload

    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token"
        )