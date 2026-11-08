from datetime import date, datetime, timezone
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Project, Task, User
from backend.app.schemas.projects import (
    ProjectCreate,
    ProjectListResponse,
    ProjectResponse,
    ProjectStatus,
    ProjectUpdate,
)
from backend.app.utils.audit import create_audit_log
from backend.app.utils.roles import require_roles

router = APIRouter(prefix="/projects", tags=["Projects"])

_PROJECT_ROLES = ("owner", "admin", "manager", "member")
_ALLOWED_STATUS_TRANSITIONS = {
    "planned": {"active", "on_hold", "cancelled"},
    "active": {"on_hold", "completed", "cancelled"},
    "on_hold": {"active", "completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def _validate_project_dates(
    project_status: str,
    start_date: date | None,
    target_deadline: date | None,
    completion_date: date | None,
) -> None:
    if start_date is not None and target_deadline is not None:
        if start_date > target_deadline:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="start_date must be earlier than or equal to target_deadline",
            )
    if project_status == "completed" and completion_date is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="completion_date is required for completed projects",
        )
    if project_status != "completed" and completion_date is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="completion_date is only valid for completed projects",
        )
    if (
        start_date is not None
        and completion_date is not None
        and completion_date < start_date
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="completion_date must be on or after start_date",
        )


def _validate_project_manager(
    db: Session,
    tenant_id: int,
    manager_id: int | None,
) -> None:
    if manager_id is None:
        return
    manager = (
        db.query(User)
        .filter(
            User.id == manager_id,
            User.tenant_id == tenant_id,
            User.role == "manager",
        )
        .first()
    )
    if manager is None:
        raise HTTPException(status_code=404, detail="Project manager not found")


@router.post("/", response_model=ProjectResponse)
def create_project(
    project: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles("owner", "admin", "manager")),
):
    tenant_id = current_user["tenant_id"]
    _validate_project_manager(db, tenant_id, project.project_manager_id)

    completion_date = project.completion_date
    if project.status == "completed" and completion_date is None:
        completion_date = datetime.now(timezone.utc).date()
    _validate_project_dates(
        project.status,
        project.start_date,
        project.target_deadline,
        completion_date,
    )

    new_project = Project(
        tenant_id=tenant_id,
        name=project.name,
        description=project.description,
        status=project.status,
        start_date=project.start_date,
        target_deadline=project.target_deadline,
        completion_date=completion_date,
        project_manager_id=project.project_manager_id,
    )

    db.add(new_project)
    db.flush()
    create_audit_log(
        db=db,
        tenant_id=tenant_id,
        user_id=int(current_user["sub"]),
        action="project_created",
        resource_type="project",
        resource_id=new_project.id,
        metadata={
            "status": new_project.status,
            "project_manager_id": new_project.project_manager_id,
        },
    )
    if new_project.project_manager_id is not None:
        create_audit_log(
            db=db,
            tenant_id=tenant_id,
            user_id=int(current_user["sub"]),
            action="project_manager_assigned",
            resource_type="project",
            resource_id=new_project.id,
            metadata={"new_project_manager_id": new_project.project_manager_id},
        )
    db.commit()
    db.refresh(new_project)
    return new_project


@router.get("/", response_model=ProjectListResponse)
def get_projects(
    status_filter: ProjectStatus | None = Query(default=None, alias="status"),
    manager_id: int | None = Query(default=None, ge=1),
    search: str | None = Query(default=None, min_length=1, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_PROJECT_ROLES)),
):
    query = db.query(Project).filter(Project.tenant_id == current_user["tenant_id"])
    if status_filter is not None:
        query = query.filter(Project.status == status_filter)
    if manager_id is not None:
        query = query.filter(Project.project_manager_id == manager_id)
    if search is not None:
        term = f"%{search.strip()}%"
        query = query.filter(
            Project.name.ilike(term) | Project.description.ilike(term)
        )

    total = query.count()
    items = (
        query.order_by(Project.created_at.desc(), Project.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": ceil(total / page_size) if total else 0,
    }


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles(*_PROJECT_ROLES)),
):
    project = (
        db.query(Project)
        .filter(
            Project.id == project_id,
            Project.tenant_id == current_user["tenant_id"],
        )
        .first()
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.put("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: int,
    project_data: ProjectUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles("owner", "admin", "manager")),
):
    project = (
        db.query(Project)
        .filter(
            Project.id == project_id,
            Project.tenant_id == current_user["tenant_id"],
        )
        .first()
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    update_data = project_data.model_dump(exclude_unset=True)
    if "project_manager_id" in update_data:
        _validate_project_manager(
            db, current_user["tenant_id"], update_data["project_manager_id"]
        )

    old_status = project.status
    old_manager_id = project.project_manager_id
    new_status = update_data.get("status", old_status)
    if new_status != old_status and new_status not in _ALLOWED_STATUS_TRANSITIONS[
        old_status
    ]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project status cannot transition from {old_status} to {new_status}",
        )

    values = {
        "start_date": update_data.get("start_date", project.start_date),
        "target_deadline": update_data.get(
            "target_deadline", project.target_deadline
        ),
        "completion_date": update_data.get(
            "completion_date", project.completion_date
        ),
    }
    generated_completion_date = False
    if new_status == "completed" and values["completion_date"] is None:
        if (
            project.status == "completed"
            and "completion_date" in update_data
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="completion_date cannot be cleared for a completed project",
            )
        values["completion_date"] = datetime.now(timezone.utc).date()
        update_data["completion_date"] = values["completion_date"]
        generated_completion_date = True
    _validate_project_dates(
        new_status,
        values["start_date"],
        values["target_deadline"],
        values["completion_date"],
    )

    changed_fields = [
        field
        for field, value in update_data.items()
        if getattr(project, field) != value
    ]
    for field in changed_fields:
        setattr(project, field, update_data[field])

    general_fields = [
        field
        for field in changed_fields
        if field not in {"status", "project_manager_id"}
        and not (generated_completion_date and field == "completion_date")
    ]
    actor_id = int(current_user["sub"])
    tenant_id = current_user["tenant_id"]
    if general_fields:
        create_audit_log(
            db=db,
            tenant_id=tenant_id,
            user_id=actor_id,
            action="project_updated",
            resource_type="project",
            resource_id=project.id,
            metadata={"changed_fields": sorted(general_fields)},
        )
    if project.status != old_status:
        create_audit_log(
            db=db,
            tenant_id=tenant_id,
            user_id=actor_id,
            action="project_status_changed",
            resource_type="project",
            resource_id=project.id,
            metadata={"old_status": old_status, "new_status": project.status},
        )
    if project.project_manager_id != old_manager_id:
        create_audit_log(
            db=db,
            tenant_id=tenant_id,
            user_id=actor_id,
            action="project_manager_assigned",
            resource_type="project",
            resource_id=project.id,
            metadata={
                "old_project_manager_id": old_manager_id,
                "new_project_manager_id": project.project_manager_id,
            },
        )

    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_roles("owner", "admin")),
):
    project = (
        db.query(Project)
        .filter(
            Project.id == project_id,
            Project.tenant_id == current_user["tenant_id"],
        )
        .first()
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if db.query(Task.id).filter(Task.project_id == project.id).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a project that still has tasks",
        )

    create_audit_log(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=int(current_user["sub"]),
        action="project_deleted",
        resource_type="project",
        resource_id=project.id,
    )
    db.delete(project)
    db.commit()
    return {"message": "Project deleted successfully"}
