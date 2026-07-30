import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.project import ProjectStatus


class ProjectCreateRequest(BaseModel):
    github_repo_url: str
    name: Optional[str] = None


class ProjectResponse(BaseModel):
    # from_attributes lets FastAPI validate the ORM object a route returns
    # directly, so no hand-written field mapping is needed.
    model_config = ConfigDict(from_attributes=True)

    # Serialises to the same opaque string the frontend already treats IDs as.
    id: uuid.UUID
    name: str
    github_repo_url: str
    github_owner: str
    github_repo_name: str
    status: ProjectStatus
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
