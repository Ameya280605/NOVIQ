from datetime import date, datetime, timezone
from math import ceil
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Project, Task, User
from backend.app.schemas.tasks import (
    TaskCreate,
    TaskListResponse,
    TaskPriority,
    TaskResponse,
    TaskStatus,
    TaskUpdate,
)
from backend.app.services.project_risk_jobs import (
    refresh_project_risk_after_mutation,
)
from backend.app.utils.audit import create_audit_log
from backend.app.utils.roles import require_roles

router = APIRouter(prefix="/tasks", tags=["Tasks"])

_TASK_ROLES = ("owner", "admin", "manager", "member")
_ALLOWED_STATUS_TRANSITIONS = {
    "todo": {"in_progress", "blocked"},
    "in_progress": {"todo", "in_review", "completed", "blocked"},
    "in_review": {"in_progress", "completed", "blocked"},
    "blocked": {"todo", "in_progress"},
    "completed": set(),
}


def _validate_task_dates(
    start_date: date | None,
    due_date: datetime | None,
) -> None:
    if start_date is None or due_date is None:
        return
    due_day = (
        due_date.astimezone(timezone.utc).date()
        if due_date.utcoffset() is not None
        else due_date.date()
    )
    if start_date > due_day:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_date must be earlier than or equal to due_date",
        )


def _get_tenant_project(db: Session, project_id: int, tenant_id: int) -> Project:
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.tenant_id == tenant_id)
        .first()
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _validate_assignee(
    db: Session,
    user_id: int | None,
    tenant_id: int,
) -> None:
    if user_id is None:
        return
    assignee = (
        db.query(User)
        .filter(User.id == user_id, User.tenant_id == tenant_id)
        .first()
    )
    if assignee is None:
        raise HTTPException(status_code=404, detail="Assignee not found")


@router.post("/", response_model=TaskResponse)
def create_task(
    task: TaskCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles("owner", "admin", "manager")),
):
    tenant_id = current_user["tenant_id"]
    _get_tenant_project(db, task.project_id, tenant_id)
    _validate_assignee(db, task.assigned_to, tenant_id)
    _validate_task_dates(task.start_date, task.due_date)

    new_task = Task(
        project_id=task.project_id,
        title=task.title,
        description=task.description,
        status=task.status,
        priority=task.priority,
        start_date=task.start_date,
        due_date=task.due_date,
        assigned_to=task.assigned_to,
    )
    db.add(new_task)
    db.flush()
    actor_id = int(current_user["sub"])
    create_audit_log(
        db=db,
        tenant_id=tenant_id,
        user_id=actor_id,
        action="task_created",
        resource_type="task",
        resource_id=new_task.id,
        metadata={"project_id": new_task.project_id},
    )
    if new_task.assigned_to is not None:
        create_audit_log(
            db=db,
            tenant_id=tenant_id,
            user_id=actor_id,
            action="task_assigned",
            resource_type="task",
            resource_id=new_task.id,
            metadata={"assigned_to": new_task.assigned_to},
        )
    db.commit()
    refresh_project_risk_after_mutation(tenant_id, {new_task.project_id})
    db.refresh(new_task)
    return new_task


@router.get("/", response_model=TaskListResponse)
def get_tasks(
    status_filter: TaskStatus | None = Query(default=None, alias="status"),
    priority_filter: TaskPriority | None = Query(default=None, alias="priority"),
    project_id: int | None = Query(default=None, ge=1),
    assigned_to: int | None = Query(default=None, ge=1),
    search: str | None = Query(default=None, min_length=1, max_length=100),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=100),
    sort_by: Literal[
        "created_at", "updated_at", "title", "status", "priority",
        "start_date", "due_date",
    ] = "created_at",
    sort_order: Literal["asc", "desc"] = "desc",
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_TASK_ROLES)),
):
    query = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(Project.tenant_id == current_user["tenant_id"])
    )
    if status_filter is not None:
        query = query.filter(Task.status == status_filter)
    if priority_filter is not None:
        query = query.filter(Task.priority == priority_filter)
    if project_id is not None:
        query = query.filter(Task.project_id == project_id)
    if assigned_to is not None:
        query = query.filter(Task.assigned_to == assigned_to)
    if search is not None:
        query = query.filter(Task.title.ilike(f"%{search.strip()}%"))

    total = query.count()
    sort_column = getattr(Task, sort_by)
    ordering = sort_column.asc() if sort_order == "asc" else sort_column.desc()
    items = (
        query.order_by(ordering, Task.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "items": items,
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": ceil(total / limit) if total else 0,
    }


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_TASK_ROLES)),
):
    task = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(
            Task.id == task_id,
            Project.tenant_id == current_user["tenant_id"],
        )
        .first()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.put("/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: int,
    task_data: TaskUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles("owner", "admin", "manager")),
):
    task = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(
            Task.id == task_id,
            Project.tenant_id == current_user["tenant_id"],
        )
        .first()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = task_data.model_dump(exclude_unset=True)
    if "project_id" in update_data:
        _get_tenant_project(db, update_data["project_id"], current_user["tenant_id"])
    if "assigned_to" in update_data:
        _validate_assignee(db, update_data["assigned_to"], current_user["tenant_id"])

    old_status = task.status
    old_assignee_id = task.assigned_to
    old_project_id = task.project_id
    new_status = update_data.get("status", old_status)
    if new_status != old_status and new_status not in _ALLOWED_STATUS_TRANSITIONS[
        old_status
    ]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task status cannot transition from {old_status} to {new_status}",
        )

    start_date = update_data.get("start_date", task.start_date)
    due_date = update_data.get("due_date", task.due_date)
    _validate_task_dates(start_date, due_date)

    changed_fields = [
        field
        for field, value in update_data.items()
        if getattr(task, field) != value
    ]
    for field in changed_fields:
        setattr(task, field, update_data[field])

    actor_id = int(current_user["sub"])
    tenant_id = current_user["tenant_id"]
    general_fields = [
        field for field in changed_fields
        if field not in {"status", "assigned_to"}
    ]
    if general_fields:
        create_audit_log(
            db=db,
            tenant_id=tenant_id,
            user_id=actor_id,
            action="task_updated",
            resource_type="task",
            resource_id=task.id,
            metadata={"changed_fields": sorted(general_fields)},
        )
    if task.status != old_status:
        create_audit_log(
            db=db,
            tenant_id=tenant_id,
            user_id=actor_id,
            action="task_status_changed",
            resource_type="task",
            resource_id=task.id,
            metadata={"old_status": old_status, "new_status": task.status},
        )
    if task.assigned_to != old_assignee_id:
        create_audit_log(
            db=db,
            tenant_id=tenant_id,
            user_id=actor_id,
            action="task_assigned",
            resource_type="task",
            resource_id=task.id,
            metadata={
                "old_assigned_to": old_assignee_id,
                "new_assigned_to": task.assigned_to,
            },
        )
    db.commit()
    if changed_fields:
        refresh_project_risk_after_mutation(
            tenant_id, {old_project_id, task.project_id}
        )
    db.refresh(task)
    return task


@router.delete("/{task_id}")
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles("owner", "admin")),
):
    task = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(
            Task.id == task_id,
            Project.tenant_id == current_user["tenant_id"],
        )
        .first()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    create_audit_log(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=int(current_user["sub"]),
        action="task_deleted",
        resource_type="task",
        resource_id=task.id,
    )
    project_id = task.project_id
    tenant_id = current_user["tenant_id"]
    db.delete(task)
    db.commit()
    refresh_project_risk_after_mutation(tenant_id, {project_id})
    return {"message": "Task deleted successfully"}
