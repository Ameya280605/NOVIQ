from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Project, Task, User
from backend.app.schemas.tasks import (
    TaskCreate,
    TaskResponse,
    TaskUpdate,
    TaskListResponse,
)
from backend.app.utils.jwt import get_current_user
from backend.app.utils.roles import require_roles
from backend.app.utils.audit import create_audit_log

router = APIRouter(prefix="/tasks", tags=["Tasks"])


@router.post("/", response_model=TaskResponse)
def create_task(
    task: TaskCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles("owner", "admin", "manager")),
):
    project = (
        db.query(Project)
        .filter(
            Project.id == task.project_id,
            Project.tenant_id == current_user["tenant_id"],
        )
        .first()
    )

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if task.assigned_to is not None:
        assignee = (
            db.query(User)
            .filter(
                User.id == task.assigned_to,
                User.tenant_id == current_user["tenant_id"],
            )
            .first()
        )
        if not assignee:
            raise HTTPException(status_code=404, detail="Assignee not found")

    new_task = Task(
        project_id=task.project_id,
        title=task.title,
        description=task.description,
        status=task.status,
        priority=task.priority,
        due_date=task.due_date,
        assigned_to=task.assigned_to,
    )

    db.add(new_task)
    db.flush()
    create_audit_log(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=int(current_user["sub"]),
        action="task_created",
        resource_type="task",
        resource_id=new_task.id,
        metadata={"project_id": new_task.project_id},
    )
    if new_task.assigned_to is not None:
        create_audit_log(
            db=db,
            tenant_id=current_user["tenant_id"],
            user_id=int(current_user["sub"]),
            action="task_assigned",
            resource_type="task",
            resource_id=new_task.id,
            metadata={"assigned_to": new_task.assigned_to},
        )
    db.commit()
    db.refresh(new_task)

    return new_task


@router.get("/", response_model=TaskListResponse)
def get_tasks(
    status: str | None = None,
    priority: str | None = None,
    project_id: int | None = None,
    assigned_to: int | None = None,
    page: int = 1,
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    query = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(Project.tenant_id == current_user["tenant_id"])
    )

    if status:
        query = query.filter(Task.status == status)

    if priority:
        query = query.filter(Task.priority == priority)

    if project_id:
        query = query.filter(Task.project_id == project_id)

    if assigned_to:
        query = query.filter(Task.assigned_to == assigned_to)

    total = query.count()

    offset = (page - 1) * limit

    tasks = query.offset(offset).limit(limit).all()

    total_pages = (total + limit - 1) // limit

    return {
        "items": tasks,
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
    }


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    task = (
        db.query(Task)
        .join(Project, Task.project_id == Project.id)
        .filter(Task.id == task_id, Project.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not task:
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
        .filter(Task.id == task_id, Project.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = task_data.model_dump(exclude_unset=True)
    if update_data.get("assigned_to") is not None:
        assignee = (
            db.query(User)
            .filter(
                User.id == update_data["assigned_to"],
                User.tenant_id == current_user["tenant_id"],
            )
            .first()
        )
        if not assignee:
            raise HTTPException(status_code=404, detail="Assignee not found")

    old_status = task.status
    old_assigned_to = task.assigned_to
    changed_fields = [
        field for field, value in update_data.items()
        if getattr(task, field) != value
    ]
    for field, value in update_data.items():
        setattr(task, field, value)

    general_changed_fields = [
        field for field in changed_fields if field not in {"status", "assigned_to"}
    ]
    if general_changed_fields:
        create_audit_log(
            db=db,
            tenant_id=current_user["tenant_id"],
            user_id=int(current_user["sub"]),
            action="task_updated",
            resource_type="task",
            resource_id=task.id,
            metadata={"changed_fields": sorted(general_changed_fields)},
        )
    if "status" in update_data and task.status != old_status:
        create_audit_log(
            db=db,
            tenant_id=current_user["tenant_id"],
            user_id=int(current_user["sub"]),
            action="task_status_changed",
            resource_type="task",
            resource_id=task.id,
            metadata={"old_status": old_status, "new_status": task.status},
        )
    if "assigned_to" in update_data and task.assigned_to != old_assigned_to:
        create_audit_log(
            db=db,
            tenant_id=current_user["tenant_id"],
            user_id=int(current_user["sub"]),
            action="task_assigned",
            resource_type="task",
            resource_id=task.id,
            metadata={
                "old_assigned_to": old_assigned_to,
                "new_assigned_to": task.assigned_to,
            },
        )

    db.commit()
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
        .filter(Task.id == task_id, Project.tenant_id == current_user["tenant_id"])
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task_id = task.id
    create_audit_log(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=int(current_user["sub"]),
        action="task_deleted",
        resource_type="task",
        resource_id=task_id,
    )
    db.delete(task)
    db.commit()

    return {"message": "Task deleted successfully"}
