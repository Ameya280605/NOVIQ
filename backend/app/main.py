from fastapi import FastAPI

from backend.app.routes.audit_logs_routes import router as audit_logs_router
from backend.app.routes.dashboard_routes import router as dashboard_router
from backend.app.routes.projects_routes import router as projects_router
from backend.app.routes.project_risk_routes import router as project_risk_router
from backend.app.routes.tasks_routes import router as tasks_router
from backend.app.routes.users_routes import router as users_router

app = FastAPI(title="NOVIQ")

app.include_router(projects_router)
app.include_router(project_risk_router)
app.include_router(tasks_router)
app.include_router(users_router)
app.include_router(audit_logs_router)
app.include_router(dashboard_router)


@app.get("/")
def root():
    return {"message": "NOVIQ API is running"}