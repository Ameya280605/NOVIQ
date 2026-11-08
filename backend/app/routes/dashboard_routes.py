from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Project, Task, User
from backend.app.schemas.dashboard import (
    DashboardOverviewResponse,
    ProjectAnalyticsResponse,
    TaskAnalyticsResponse,
    TeamAnalyticsResponse,
)
from backend.app.utils.roles import require_roles

router = APIRouter(prefix="/dashboard", tags=["Dashboard & Analytics"])
_TENANT_ROLES = ("owner", "admin", "manager", "member")


def _validate_date_range(
    start_date: date | None,
    end_date: date | None,
) -> None:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_date must be earlier than or equal to end_date",
        )
    if end_date == date.max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="end_date is out of range",
        )
    if start_date is not None and end_date is not None:
        if (end_date - start_date).days > 365:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Date range cannot exceed 366 days",
            )


def _date_range(
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    end = end_date or today
    default_start = (
        end - timedelta(days=29)
        if end > date.min + timedelta(days=29)
        else date.min
    )
    start = start_date or default_start
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_date must be earlier than or equal to end_date",
        )
    _validate_date_range(start, end)
    return start, end


def _datetime_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(
        end_date + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return start, end_exclusive


def _created_at_filters(
    column: Any,
    start_date: date | None,
    end_date: date | None,
) -> list[Any]:
    conditions = []
    if start_date is not None:
        conditions.append(column >= datetime.combine(start_date, time.min, timezone.utc))
    if end_date is not None:
        conditions.append(
            column
            < datetime.combine(end_date + timedelta(days=1), time.min, timezone.utc)
        )
    return conditions


@router.get("/overview", response_model=DashboardOverviewResponse)
def get_dashboard_overview(
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_TENANT_ROLES)),
):
    _validate_date_range(start_date, end_date)
    tenant_id = current_user["tenant_id"]
    project_query = db.query(Project).filter(Project.tenant_id == tenant_id)
    for condition in _created_at_filters(
        Project.created_at, start_date, end_date
    ):
        project_query = project_query.filter(condition)
    project_counts = project_query.with_entities(
        func.count(Project.id),
        func.sum(case((Project.status == "active", 1), else_=0)),
        func.sum(case((Project.status == "completed", 1), else_=0)),
    ).one()
    total_projects = int(project_counts[0] or 0)
    active_projects = int(project_counts[1] or 0)
    completed_projects = int(project_counts[2] or 0)

    task_query = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(Project.tenant_id == tenant_id)
    )
    for condition in _created_at_filters(Task.created_at, start_date, end_date):
        task_query = task_query.filter(condition)
    now = datetime.now(timezone.utc)
    task_counts = task_query.with_entities(
        func.count(Task.id),
        func.sum(case((Task.status == "completed", 1), else_=0)),
        func.sum(case((Task.status != "completed", 1), else_=0)),
        func.sum(
            case(
                (
                    and_(
                        Task.status != "completed",
                        Task.due_date.is_not(None),
                        Task.due_date < now,
                    ),
                    1,
                ),
                else_=0,
            )
        ),
    ).one()
    total_tasks = int(task_counts[0] or 0)
    completed_tasks = int(task_counts[1] or 0)
    pending_tasks = int(task_counts[2] or 0)
    overdue_tasks = int(task_counts[3] or 0)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "projects": {
            "total": total_projects,
            "active": active_projects,
            "completed": completed_projects,
            "completion_percentage": (
                round(completed_projects * 100 / total_projects, 2)
                if total_projects
                else 0.0
            ),
        },
        "tasks": {
            "total": total_tasks,
            "completed": completed_tasks,
            "pending": pending_tasks,
            "overdue": overdue_tasks,
            "completion_percentage": (
                round(completed_tasks * 100 / total_tasks, 2)
                if total_tasks
                else 0.0
            ),
        },
    }


@router.get("/tasks", response_model=TaskAnalyticsResponse)
def get_task_analytics(
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_TENANT_ROLES)),
):
    start, end = _date_range(start_date, end_date)
    tenant_id = current_user["tenant_id"]
    start_at, end_at = _datetime_bounds(start, end)

    scoped_tasks = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(
            Project.tenant_id == tenant_id,
            Task.created_at >= start_at,
            Task.created_at < end_at,
        )
    )
    status_rows = (
        scoped_tasks.with_entities(Task.status, func.count(Task.id))
        .group_by(Task.status)
        .order_by(Task.status)
        .all()
    )
    priority_rows = (
        scoped_tasks.with_entities(Task.priority, func.count(Task.id))
        .group_by(Task.priority)
        .order_by(Task.priority)
        .all()
    )

    tenant_task_query = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(Project.tenant_id == tenant_id)
    )
    today_start = datetime.combine(
        datetime.now(timezone.utc).date(), time.min, timezone.utc
    )
    overdue_rows = (
        tenant_task_query
        .filter(
            Task.status != "completed",
            Task.due_date.is_not(None),
            Task.due_date < datetime.now(timezone.utc),
            Task.due_date >= start_at,
            Task.due_date < end_at,
        )
        .with_entities(
            Task.id,
            Task.title,
            Task.project_id,
            Project.name,
            Task.assigned_to,
            Task.status,
            Task.priority,
            Task.due_date,
        )
        .order_by(Task.due_date.asc(), Task.id.asc())
        .limit(100)
        .all()
    )
    today = datetime.now(timezone.utc).date()
    deadline_start = max(today, start)
    deadline_end = end_date or today + timedelta(days=30)
    upcoming_rows = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(
            Project.tenant_id == tenant_id,
            Task.status != "completed",
            Task.due_date.is_not(None),
            Task.due_date >= max(today_start, datetime.combine(
                deadline_start, time.min, timezone.utc
            )),
            Task.due_date
            < datetime.combine(
                deadline_end + timedelta(days=1), time.min, timezone.utc
            ),
        )
        .with_entities(
            Task.id,
            Task.title,
            Task.project_id,
            Project.name,
            Task.assigned_to,
            Task.status,
            Task.priority,
            Task.due_date,
        )
        .order_by(Task.due_date.asc(), Task.id.asc())
        .limit(100)
        .all()
    )

    trend_rows = (
        db.query(func.date(Task.updated_at), func.count(Task.id))
        .join(Project, Task.project_id == Project.id)
        .filter(
            Project.tenant_id == tenant_id,
            Task.status == "completed",
            Task.updated_at >= start_at,
            Task.updated_at < end_at,
        )
        .group_by(func.date(Task.updated_at))
        .all()
    )
    trend_counts = {str(day): int(count) for day, count in trend_rows}
    trend = [
        {"date": start + timedelta(days=offset),
         "count": trend_counts.get((start + timedelta(days=offset)).isoformat(), 0)}
        for offset in range((end - start).days + 1)
    ]

    def deadline_payload(row) -> dict[str, Any]:
        return {
            "task_id": row[0],
            "title": row[1],
            "project_id": row[2],
            "project_name": row[3],
            "assigned_to": row[4],
            "status": row[5],
            "priority": row[6],
            "due_date": row[7],
        }

    return {
        "start_date": start,
        "end_date": end,
        "status_distribution": [
            {"name": name, "count": int(count)} for name, count in status_rows
        ],
        "priority_distribution": [
            {"name": name, "count": int(count)} for name, count in priority_rows
        ],
        "overdue_tasks": [deadline_payload(row) for row in overdue_rows],
        "upcoming_deadlines": [deadline_payload(row) for row in upcoming_rows],
        "completion_trend": trend,
    }


@router.get("/projects", response_model=ProjectAnalyticsResponse)
def get_project_analytics(
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_TENANT_ROLES)),
):
    start, end = _date_range(start_date, end_date)
    tenant_id = current_user["tenant_id"]
    start_at, end_at = _datetime_bounds(start, end)

    project_rows = (
        db.query(Project.id, Project.name, Project.status, Project.target_deadline)
        .filter(
            Project.tenant_id == tenant_id,
            * _created_at_filters(Project.created_at, start, end),
        )
        .order_by(Project.name.asc(), Project.id.asc())
        .all()
    )
    status_rows = (
        db.query(Project.status, func.count(Project.id))
        .filter(
            Project.tenant_id == tenant_id,
            * _created_at_filters(Project.created_at, start, end),
        )
        .group_by(Project.status)
        .order_by(Project.status)
        .all()
    )

    project_ids = [row[0] for row in project_rows]
    task_count_rows = (
        db.query(
            Task.project_id,
            func.count(Task.id),
            func.sum(case((Task.status == "completed", 1), else_=0)),
        )
        .join(Project, Task.project_id == Project.id)
        .filter(
            Project.tenant_id == tenant_id,
            Project.id.in_(project_ids) if project_ids else False,
        )
        .group_by(Task.project_id)
        .all()
    )
    task_counts = {
        project_id: (int(total or 0), int(completed or 0))
        for project_id, total, completed in task_count_rows
    }
    progress = []
    for project_id, name, project_status, deadline in project_rows:
        total, completed = task_counts.get(project_id, (0, 0))
        progress.append(
            {
                "project_id": project_id,
                "name": name,
                "status": project_status,
                "total_tasks": total,
                "completed_tasks": completed,
                "completion_percentage": (
                    round(completed * 100 / total, 2) if total else 0.0
                ),
                "target_deadline": deadline,
            }
        )

    today = datetime.now(timezone.utc).date()
    deadline_start = max(today, start)
    deadline_end = end_date or today + timedelta(days=30)
    upcoming_projects = (
        db.query(Project.id, Project.name, Project.status, Project.target_deadline)
        .filter(
            Project.tenant_id == tenant_id,
            Project.target_deadline.is_not(None),
            Project.status.notin_(("completed", "cancelled")),
            Project.target_deadline >= deadline_start,
            Project.target_deadline <= deadline_end,
        )
        .order_by(Project.target_deadline.asc(), Project.id.asc())
        .limit(100)
        .all()
    )
    return {
        "start_date": start,
        "end_date": end,
        "status_distribution": [
            {"name": name, "count": int(count)} for name, count in status_rows
        ],
        "progress_by_project": progress,
        "upcoming_deadlines": [
            {
                "project_id": row[0],
                "name": row[1],
                "status": row[2],
                "target_deadline": row[3],
            }
            for row in upcoming_projects
        ],
    }


@router.get("/team", response_model=TeamAnalyticsResponse)
def get_team_analytics(
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_TENANT_ROLES)),
):
    start, end = _date_range(start_date, end_date)
    start_at, end_at = _datetime_bounds(start, end)
    tenant_id = current_user["tenant_id"]
    task_join = and_(
        Task.assigned_to == User.id,
        Task.created_at >= start_at,
        Task.created_at < end_at,
    )
    project_join = and_(
        Task.project_id == Project.id,
        Project.tenant_id == tenant_id,
    )
    rows = (
        db.query(
            User.id,
            User.name,
            User.role,
            func.count(Task.id),
            func.sum(case((Task.status == "completed", 1), else_=0)),
            func.sum(
                case(
                    (Task.id.is_not(None) & (Task.status != "completed"), 1),
                    else_=0,
                )
            ),
        )
        .filter(User.tenant_id == tenant_id)
        .outerjoin(Task, task_join)
        .outerjoin(Project, project_join)
        .filter((Task.id.is_(None)) | (Project.id.is_not(None)))
        .group_by(User.id, User.name, User.role)
        .order_by(User.name.asc(), User.id.asc())
        .all()
    )
    workload_total = sum(int(row[3] or 0) for row in rows)
    return {
        "start_date": start,
        "end_date": end,
        "members": [
            {
                "user_id": row[0],
                "name": row[1],
                "role": row[2],
                "assigned_tasks": int(row[3] or 0),
                "completed_tasks": int(row[4] or 0),
                "pending_tasks": int(row[5] or 0),
                "workload_percentage": (
                    round(int(row[3] or 0) * 100 / workload_total, 2)
                    if workload_total
                    else 0.0
                ),
            }
            for row in rows
        ],
    }
