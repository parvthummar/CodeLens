from fastapi import APIRouter

from app.api.v1 import auth, projects

v1_router = APIRouter(prefix="/api/v1")

v1_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
v1_router.include_router(projects.router, prefix="/projects", tags=["Projects"])
