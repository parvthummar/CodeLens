"""Durable state for one indexing run.

Redis (via ARQ) delivers jobs to a worker; this table is what a job *is*. The
split matters because the two stores can disagree — a Redis enqueue can fail
after the project row commits, and Redis is not durable storage. Keeping job
state here means:

- a job that never reached Redis is still visible as `queued`, and the startup
  reconciler re-delivers it
- claiming is an atomic UPDATE in Postgres, so two workers pulling the same
  Redis message still cannot run the same job twice
- attempts, backoff and failure history survive a Redis flush and are queryable
  with plain SQL
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.project import Project


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IndexingJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "indexing_jobs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Same rationale as Project.status: VARCHAR + CHECK, so adding a state later
    # is an ordinary constraint change rather than an ALTER TYPE.
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(
            JobStatus,
            name="indexing_job_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
        index=True,
    )

    # Incremented when the job is claimed, not when it fails, so a worker killed
    # mid-run still burns an attempt. Otherwise a job that reliably kills its
    # worker would be retried forever.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Not before this time. Backoff moves it forward; the reconciler and the
    # claim both respect it.
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Written by the running worker every few seconds. A stale value is the only
    # evidence available that a worker died — the process is gone, so it cannot
    # report anything itself.
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Checkpoint counters, updated once per chunk. Progress that a retry does
    # not have to guess at, and what the UI can show for a large repo.
    entities_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    entities_done: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    project: Mapped[Project] = relationship(lazy="raise")

    def __repr__(self) -> str:
        return f"<IndexingJob {self.id} {self.status} attempt={self.attempts}>"
