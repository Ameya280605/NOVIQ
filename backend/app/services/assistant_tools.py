from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend.app.models import Project, Task, User
from backend.app.services.project_risk import analyze_project_risk


class AssistantAccessDenied(Exception):
    pass


class AssistantResourceNotFound(Exception):
    pass


def _authorize_tool(db: Session, tenant_id: int, user_id: int) -> None:
    actor = (
        db.query(User.id)
        .filter(User.id == user_id, User.tenant_id == tenant_id)
        .first()
    )
    if actor is None:
        raise AssistantAccessDenied("Authenticated user is not in this tenant")


def _tenant_project(
    db: Session,
    tenant_id: int,
    project_id: int,
) -> Project:
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.tenant_id == tenant_id)
        .first()
    )
    if project is None:
        raise AssistantResourceNotFound("Project not found")
    return project


def project_status(
    db: Session,
    tenant_id: int,
    user_id: int,
    project_id: int | None = None,
) -> list[dict[str, Any]]:
    _authorize_tool(db, tenant_id, user_id)
    query = db.query(Project).filter(Project.tenant_id == tenant_id)
    if project_id is not None:
        _tenant_project(db, tenant_id, project_id)
        query = query.filter(Project.id == project_id)
    projects = query.order_by(Project.id.asc()).limit(50).all()
    return [
        {
            "project_id": project.id,
            "name": project.name,
            "status": project.status,
            "target_deadline": project.target_deadline,
        }
        for project in projects
    ]


def overdue_tasks(
    db: Session,
    tenant_id: int,
    user_id: int,
    project_id: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    _authorize_tool(db, tenant_id, user_id)
    current_time = now or datetime.now(timezone.utc)
    query = (
        db.query(Task, Project)
        .join(Project, Task.project_id == Project.id)
        .filter(
            Project.tenant_id == tenant_id,
            Task.status != "completed",
            Task.due_date.is_not(None),
            Task.due_date < current_time,
        )
    )
    if project_id is not None:
        _tenant_project(db, tenant_id, project_id)
        query = query.filter(Project.id == project_id)
    rows = query.order_by(Task.due_date.asc(), Task.id.asc()).limit(50).all()
    tenant_user_ids = {
        row[0]
        for row in db.query(User.id).filter(User.tenant_id == tenant_id).all()
    }
    return [
        {
            "task_id": task.id,
            "title": task.title,
            "project_id": project.id,
            "project_name": project.name,
            "status": task.status,
            "due_date": task.due_date,
            "assigned_to": (
                task.assigned_to if task.assigned_to in tenant_user_ids else None
            ),
        }
        for task, project in rows
    ]


def team_workload(
    db: Session,
    tenant_id: int,
    user_id: int,
) -> list[dict[str, Any]]:
    _authorize_tool(db, tenant_id, user_id)
    rows = (
        db.query(User.id, func.count(Task.id))
        .outerjoin(
            Task,
            (Task.assigned_to == User.id) & (Task.status != "completed"),
        )
        .outerjoin(Project, Task.project_id == Project.id)
        .filter(
            User.tenant_id == tenant_id,
            (Task.id.is_(None)) | (Project.tenant_id == tenant_id),
        )
        .group_by(User.id)
        .order_by(User.id.asc())
        .all()
    )
    return [
        {"user_id": member_id, "open_assigned_tasks": int(count or 0)}
        for member_id, count in rows
    ]


def project_risk(
    db: Session,
    tenant_id: int,
    user_id: int,
    project_id: int,
) -> dict[str, Any]:
    _authorize_tool(db, tenant_id, user_id)
    project = _tenant_project(db, tenant_id, project_id)
    result = analyze_project_risk(db, project)
    return {
        "project_id": result["project_id"],
        "risk_score": result["risk_score"],
        "risk_label": result["risk_label"],
        "reasons": result["reasons"],
        "bottlenecks": result["bottlenecks"],
        "scoring_version": result["scoring_version"],
    }


def project_summary(
    db: Session,
    tenant_id: int,
    user_id: int,
    project_id: int,
) -> dict[str, Any]:
    _authorize_tool(db, tenant_id, user_id)
    project = _tenant_project(db, tenant_id, project_id)
    task_count, completed_count = (
        db.query(
            func.count(Task.id),
            func.sum(case((Task.status == "completed", 1), else_=0)),
        )
        .filter(Task.project_id == project.id)
        .one()
    )
    now = datetime.now(timezone.utc)
    overdue = (
        db.query(Task)
        .filter(
            Task.project_id == project.id,
            Task.status != "completed",
            Task.due_date.is_not(None),
            Task.due_date < now,
        )
        .order_by(Task.due_date.asc(), Task.id.asc())
        .limit(50)
        .all()
    )
    risk = project_risk(db, tenant_id, user_id, project_id)
    return {
        "project_id": project.id,
        "name": project.name,
        "status": project.status,
        "target_deadline": project.target_deadline,
        "task_count": int(task_count or 0),
        "completed_tasks": int(completed_count or 0),
        "overdue_tasks": [
            {"task_id": task.id, "title": task.title, "due_date": task.due_date}
            for task in overdue[:50]
        ],
        "risk": risk,
    }
