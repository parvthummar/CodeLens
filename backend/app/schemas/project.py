from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from app.models.project import Project

class ProjectCreateRequest(BaseModel):
    github_repo_url: str
    name: Optional[str] = None

class ProjectResponse(BaseModel):
    id: str
    name: str
    github_repo_url: str
    github_owner: str
    github_repo_name: str
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_document(cls, project: Project) -> "ProjectResponse":
        return cls(
            id=str(project.id),
            name=project.name,
            github_repo_url=project.github_repo_url,
            github_owner=project.github_owner,
            github_repo_name=project.github_repo_name,
            status=project.status,
            error_message=project.error_message,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
