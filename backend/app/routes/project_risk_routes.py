from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import Project
from backend.app.schemas.project_risk import ProjectRiskResponse
from backend.app.services.project_risk import analyze_project_risk
from backend.app.utils.roles import require_roles

router = APIRouter(prefix="/projects", tags=["Project Risk Analysis"])
_PROJECT_ROLES = ("owner", "admin", "manager", "member")


@router.get("/{project_id}/risk-analysis", response_model=ProjectRiskResponse)
def get_project_risk_analysis(
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
    return analyze_project_risk(db, project)
