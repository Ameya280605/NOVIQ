from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditLogResponse(BaseModel):
    id: int
    tenant_id: int
    user_id: int
    action: str
    resource_type: str
    resource_id: int | None
    details: str | None
    metadata: dict[str, Any] | None = Field(validation_alias="metadata_json")
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    page: int
    page_size: int
    total: int
    total_pages: int