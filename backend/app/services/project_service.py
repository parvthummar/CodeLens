import re
from urllib.parse import urlparse
from fastapi import HTTPException, status
from bson import ObjectId
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

async def create_project(user: User, data: ProjectCreateRequest) -> Project:
    try:
        owner, repo_name = parse_github_url(data.github_repo_url)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    
    name = data.name or repo_name
    
    project = Project(
        name=name,
        github_repo_url=data.github_repo_url,
        github_owner=owner,
        github_repo_name=repo_name,
        status=ProjectStatus.PENDING,
        user_id=str(user.id),
        pinecone_namespace="",
    )
    await project.insert()
    
    project.pinecone_namespace = str(project.id)
    await project.save()
    
    return project

async def list_projects(user: User) -> list[Project]:
    return await Project.find({"user_id": str(user.id)}).to_list()

async def get_project(user: User, project_id: str) -> Project:
    try:
        obj_id = ObjectId(project_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project ID")
        
    project = await Project.find_one({"_id": obj_id, "user_id": str(user.id)})
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project

async def delete_project(user: User, project_id: str) -> None:
    project = await get_project(user, project_id)
    
    if project.pinecone_namespace:
        try:
            await delete_namespace(project.pinecone_namespace)
        except Exception:
            pass
            
    await project.delete()
