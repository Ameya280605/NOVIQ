from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Project
from backend.app.schemas.projects import ProjectCreate, ProjectResponse


router = APIRouter(
    prefix="/projects",
    tags=["Projects"]
)


@router.post("/", response_model=ProjectResponse)
def create_project(
    project: ProjectCreate,
    db: Session = Depends(get_db)
):
    new_project = Project(
        tenant_id=2,
        name=project.name,
        description=project.description
    )

    db.add(new_project)
    db.commit()
    db.refresh(new_project)

    return new_project