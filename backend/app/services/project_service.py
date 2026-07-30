import uuid
from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.schemas.project import ProjectCreateRequest
from app.services.pinecone_service import delete_namespace


def parse_github_url(url: str) -> tuple[str, str]:
    url = url.rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]

    parsed = urlparse(url)
    path_parts = parsed.path.strip('/').split('/')
    if len(path_parts) >= 2:
        return path_parts[0], path_parts[1]

    raise ValueError(f"Invalid GitHub URL: {url}")


async def create_project(
    db: AsyncSession, user: User, data: ProjectCreateRequest
) -> Project:
    try:
        owner, repo_name = parse_github_url(data.github_repo_url)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Generating the ID here rather than letting the column default fire at
    # flush time means pinecone_namespace can be set in the same INSERT.
    project_id = uuid.uuid4()
    project = Project(
        id=project_id,
        user_id=user.id,
        name=data.name or repo_name,
        github_repo_url=data.github_repo_url,
        github_owner=owner,
        github_repo_name=repo_name,
        pinecone_namespace=str(project_id),
        status=ProjectStatus.PENDING,
    )
    db.add(project)
    await db.commit()
    return project


async def list_projects(db: AsyncSession, user: User) -> list[Project]:
    result = await db.execute(
        select(Project)
        .where(Project.user_id == user.id)
        .order_by(Project.created_at.desc())
    )
    return list(result.scalars().all())


async def get_project(db: AsyncSession, user: User, project_id: str) -> Project:
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project ID"
        )

    result = await db.execute(
        select(Project).where(Project.id == pid, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return project


async def delete_project(db: AsyncSession, user: User, project_id: str) -> None:
    project = await get_project(db, user, project_id)

    if project.pinecone_namespace:
        try:
            await delete_namespace(project.pinecone_namespace)
        except Exception:
            pass

    await db.delete(project)
    await db.commit()
