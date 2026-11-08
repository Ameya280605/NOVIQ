from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ProjectRiskReason(BaseModel):
    code: str
    message: str
    count: int
    points: int


class ProjectRiskBottleneck(BaseModel):
    type: Literal["repeated_blocked_task", "concentrated_workload"]
    title: str
    task_ids: list[int]
    assignee_id: int | None
    count: int
    recommendation: str


class ProjectRiskMetrics(BaseModel):
    open_tasks: int
    overdue_tasks: int
    due_soon_tasks: int
    blocked_tasks: int
    stale_tasks: int
    assigned_open_tasks: int
    top_assignee_open_tasks: int


class ProjectRiskResponse(BaseModel):
    project_id: int
    project_name: str
    project_status: str
    risk_score: int
    risk_label: Literal["healthy", "watch", "at_risk"]
    reasons: list[ProjectRiskReason]
    bottlenecks: list[ProjectRiskBottleneck]
    metrics: ProjectRiskMetrics
    generated_at: datetime
    scoring_version: str
