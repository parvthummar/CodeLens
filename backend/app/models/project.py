from enum import Enum
from beanie import Document, Indexed
from pydantic import Field
from datetime import datetime, timezone
from bson import ObjectId


class ProjectStatus(str, Enum):
    PENDING = "pending"
    CLONING = "cloning"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"


class Project(Document):
    user_id: Indexed(str)
    name: str
    github_repo_url: str
    github_owner: str
    github_repo_name: str
    pinecone_namespace: Indexed(str, unique=True)
    status: ProjectStatus = ProjectStatus.PENDING
    error_message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "projects"
