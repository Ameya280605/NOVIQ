import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal

from backend.app.utils.security import MAX_PASSWORD_BYTES


class UserCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def email_must_be_valid(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email must be valid")
        return value

    @field_validator("password")
    @classmethod
    def password_must_fit_bcrypt(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError("password must be at most 72 UTF-8 bytes")
        return value


class TeamMemberCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["owner", "admin", "manager", "member"] = "member"

    @field_validator("email")
    @classmethod
    def email_must_be_valid(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email must be valid")
        return value

    @field_validator("password")
    @classmethod
    def password_must_fit_bcrypt(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError("password must be at most 72 UTF-8 bytes")
        return value


class RoleUpdate(BaseModel):
    role: Literal["owner", "admin", "manager", "member"]


class UserLogin(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def email_must_be_valid(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email must be valid")
        return value


class UserResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    email: str
    role: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
