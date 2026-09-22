from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TaskCreate(BaseModel):
    project_id: int
    title: str
    description: str | None = None
    status: str = "todo"
    priority: str = "medium"
    due_date: datetime | None = None
    assigned_to: int | None = None


class TaskResponse(BaseModel):
    id: int
    project_id: int
    title: str
    description: str | None
    status: str
    priority: str
    due_date: datetime | None
    assigned_to: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
    
class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    due_date: datetime | None = None
    assigned_to: int | None = None