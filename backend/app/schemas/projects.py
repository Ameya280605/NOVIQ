from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProjectStatus = Literal["planned", "active", "on_hold", "completed", "cancelled"]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    status: ProjectStatus = "planned"
    start_date: date | None = None
    target_deadline: date | None = None
    completion_date: date | None = None
    project_manager_id: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(extra="forbid")


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    status: ProjectStatus | None = None
    start_date: date | None = None
    target_deadline: date | None = None
    completion_date: date | None = None
    project_manager_id: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def name_cannot_be_null(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("name cannot be null")
        return value

    @field_validator("status")
    @classmethod
    def status_cannot_be_null(cls, value: ProjectStatus | None) -> ProjectStatus:
        if value is None:
            raise ValueError("status cannot be null")
        return value


class ProjectResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    description: str | None
    status: ProjectStatus
    start_date: date | None
    target_deadline: date | None
    completion_date: date | None
    project_manager_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    page: int
    page_size: int
    total: int
    total_pages: int
