from datetime import date, datetime
from pydantic import BaseModel


class DashboardOverviewResponse(BaseModel):
    start_date: date | None
    end_date: date | None
    projects: dict[str, int | float]
    tasks: dict[str, int | float]


class BreakdownItem(BaseModel):
    name: str
    count: int


class TrendItem(BaseModel):
    date: date
    count: int


class TaskDeadlineItem(BaseModel):
    task_id: int
    title: str
    project_id: int
    project_name: str
    assigned_to: int | None
    status: str
    priority: str
    due_date: datetime


class TaskAnalyticsResponse(BaseModel):
    start_date: date
    end_date: date
    status_distribution: list[BreakdownItem]
    priority_distribution: list[BreakdownItem]
    overdue_tasks: list[TaskDeadlineItem]
    upcoming_deadlines: list[TaskDeadlineItem]
    completion_trend: list[TrendItem]


class ProjectProgressItem(BaseModel):
    project_id: int
    name: str
    status: str
    total_tasks: int
    completed_tasks: int
    completion_percentage: float
    target_deadline: date | None


class ProjectDeadlineItem(BaseModel):
    project_id: int
    name: str
    status: str
    target_deadline: date


class ProjectAnalyticsResponse(BaseModel):
    start_date: date
    end_date: date
    status_distribution: list[BreakdownItem]
    progress_by_project: list[ProjectProgressItem]
    upcoming_deadlines: list[ProjectDeadlineItem]


class TeamMemberAnalyticsItem(BaseModel):
    user_id: int
    name: str
    role: str
    assigned_tasks: int
    completed_tasks: int
    pending_tasks: int
    workload_percentage: float


class TeamAnalyticsResponse(BaseModel):
    start_date: date
    end_date: date
    members: list[TeamMemberAnalyticsItem]
