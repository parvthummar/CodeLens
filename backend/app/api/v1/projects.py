from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.postgres import get_db
from app.models.user import User
from app.schemas.project import ProjectCreateRequest, ProjectResponse
from app.schemas.search import SearchRequest, SearchResponse
from app.services import project_service, queue_service, search_service

router = APIRouter()


@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project, job_id = await project_service.create_project(db, current_user, data)
    # Enqueue after the commit, never before: a worker is fast enough to claim
    # the job and look for a project that has not been written yet. If this
    # fails the row is still `queued`, and the worker's startup reconciler
    # delivers it — the request does not fail for a queue hiccup.
    await queue_service.enqueue_job(job_id)
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


@router.post("/{project_id}/reindex", response_model=ProjectResponse)
async def reindex_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Queue another indexing run for a project that already exists."""
    project, job_id = await project_service.request_reindex(db, current_user, project_id)
    await queue_service.enqueue_job(job_id)
    return project


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
        results = await search_service.search_project(db, project, body.query, body.top_k)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return SearchResponse(results=results)
