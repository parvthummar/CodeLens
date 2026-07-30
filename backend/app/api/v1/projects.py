from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.postgres import get_db
from app.models.user import User
from app.schemas.project import ProjectCreateRequest, ProjectResponse
from app.schemas.search import SearchRequest, SearchResponse
from app.services import project_service, search_service
from app.services.indexing_service import run_indexing_pipeline

router = APIRouter()


@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreateRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await project_service.create_project(db, current_user, data)
    # Kick off the indexing pipeline in the background. It opens its own
    # sessions — the request-scoped one closes as soon as this returns.
    background_tasks.add_task(run_indexing_pipeline, str(project.id))
    return project


@router.get("/", response_model=list[ProjectResponse])
async def list_projects(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await project_service.list_projects(db, current_user)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await project_service.get_project(db, current_user, project_id)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await project_service.delete_project(db, current_user, project_id)


@router.post("/{project_id}/search", response_model=SearchResponse)
async def search_project(
    project_id: str,
    body: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await project_service.get_project(db, current_user, project_id)
    try:
        results = await search_service.search_project(project, body.query, body.top_k)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return SearchResponse(results=results)
