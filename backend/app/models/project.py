import uuid
from enum import Enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.user import User


class ProjectStatus(str, Enum):
    # Created but not yet handed to the queue. Only reachable now if the API
    # dies between the INSERT and the enqueue; kept because rows predating the
    # worker still carry it, and the reconciler treats it as queued.
    PENDING = "pending"
    QUEUED = "queued"
    CLONING = "cloning"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"


class Project(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "projects"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Declared so the unit of work knows projects depend on users and orders
    # INSERTs accordingly — without it, mappers sort alphabetically and a flush
    # containing a new user and a new project violates the FK. lazy="raise"
    # keeps this from emitting an implicit lazy load, which under asyncio fails
    # with MissingGreenlet rather than just being slow.
    owner: Mapped[User] = relationship(lazy="raise")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    github_repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    github_owner: Mapped[str] = mapped_column(String(255), nullable=False)
    github_repo_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Always str(id). Kept as a column so the Pinecone contract stays explicit
    # rather than implied by convention.
    pinecone_namespace: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )

    # native_enum=False stores this as VARCHAR + CHECK, so adding a status later
    # (queued, stale) is an ordinary constraint change rather than ALTER TYPE.
    # values_callable persists the lowercase *values*; SQLAlchemy would
    # otherwise store the member names, which the frontend does not expect.
    status: Mapped[ProjectStatus] = mapped_column(
        SAEnum(
            ProjectStatus,
            name="project_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=ProjectStatus.PENDING,
        server_default=ProjectStatus.PENDING.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # HEAD of the last run that completed. Written only on success, so it is a
    # statement about what is actually in the two stores, not about what was
    # attempted. 40 chars for a hex sha1; unset until the first run finishes.
    last_indexed_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)

    def __repr__(self) -> str:
        return f"<Project {self.github_owner}/{self.github_repo_name} {self.status}>"
