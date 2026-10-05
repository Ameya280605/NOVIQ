from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserCreate(BaseModel):
    company_name: str
    name: str
    email: str
    password: str
    

class TeamMemberCreate(BaseModel):
    name: str
    email: str
    password: str
    role: str = "member"


class RoleUpdate(BaseModel):
    role: str


class UserLogin(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    email: str
    role: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
