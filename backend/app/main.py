from fastapi import FastAPI
from backend.app.routes.projects_routes import router as projects_router
from backend.app.routes.tasks_routes import router as tasks_router
from backend.app.routes.users_routes import router as users_router

app = FastAPI(title="NOVIQ")

app.include_router(projects_router)
app.include_router(tasks_router)
app.include_router(users_router)

@app.get("/")
def root():
    return {"message": "NOVIQ API is running"}