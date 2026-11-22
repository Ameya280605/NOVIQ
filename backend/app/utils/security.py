from passlib.context import CryptContext


MAX_PASSWORD_BYTES = 72

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)


def _validate_password_size(password: str) -> None:
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError("password must be at most 72 UTF-8 bytes")


def hash_password(password: str) -> str:
    _validate_password_size(password)
    return pwd_context.hash(password)


def verify_password(
    plain_password: str,
    hashed_password: str
) -> bool:
    try:
        _validate_password_size(plain_password)
    except ValueError:
        return False
    return pwd_context.verify(
        plain_password,
        hashed_password
    )