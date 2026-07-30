"""Job lifecycle, owned by Postgres.

ARQ delivers a job id to a worker. Everything about whether that job may run,
how many times it has been tried, and what to do when a worker dies is decided
here, in SQL. That split is deliberate: Redis is a delivery mechanism and is not
durable, so it cannot be the record of what work exists.

The two guarantees this module provides:

- **A job runs at most once at a time.** `claim` is a single conditional UPDATE.
  Two workers handed the same message both run it; exactly one gets a row back.
- **No job is stranded by a dead worker.** A worker that dies stops writing its
  heartbeat, and `reconcile` requeues (or terminally fails) anything whose
  heartbeat has gone stale.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.indexing_job import IndexingJob, JobStatus
from app.models.project import Project, ProjectStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_stale_running(now: datetime):
    """A running job whose worker has stopped reporting.

    A NULL heartbeat counts as stale. `claim` always writes one, so NULL on a
    running row means something wrote the status without ever starting work —
    treating it as live would strand the job permanently, which is the exact bug
    this table exists to prevent.
    """
    stale_before = now - timedelta(seconds=settings.job_heartbeat_timeout_seconds)
    return (IndexingJob.status == JobStatus.RUNNING) & (
        or_(
            IndexingJob.heartbeat_at.is_(None),
            IndexingJob.heartbeat_at < stale_before,
        )
    )


def backoff_delay(attempts: int) -> timedelta:
    """Exponential backoff, capped.

    `attempts` is the number already made, so the first retry waits base**1.
    The cap matters more than the curve: without it a job that has failed six
    times schedules itself beyond any sensible operational horizon.
    """
    seconds = settings.job_backoff_base_seconds * (2 ** max(attempts - 1, 0))
    return timedelta(seconds=min(seconds, settings.job_backoff_max_seconds))


async def create_job(db: AsyncSession, project_id: uuid.UUID) -> IndexingJob:
    """Enqueue a run. Caller commits — see the module docstring in queue_service.

    Not committing here is the point: the caller puts this INSERT in the same
    transaction as the project row, so a project can never exist with no job.
    """
    job = IndexingJob(
        id=uuid.uuid4(),
        project_id=project_id,
        status=JobStatus.QUEUED,
        run_after=_now(),
    )
    db.add(job)
    return job


async def claim(db: AsyncSession, job_id: uuid.UUID) -> IndexingJob | None:
    """Take ownership of a job, or return None if it is not ours to run.

    The WHERE clause is the whole concurrency story. A job is claimable if it is
    queued and due, or if it is marked running but its worker has stopped
    heartbeating. Anything else — already succeeded, already failed, or actively
    running elsewhere — matches nothing and this returns None.

    `attempts` increments on claim rather than on failure so that a job which
    kills its worker outright still burns an attempt; counting only clean
    failures would retry such a job forever.
    """
    now = _now()

    stmt = (
        update(IndexingJob)
        .where(
            IndexingJob.id == job_id,
            IndexingJob.run_after <= now,
            or_(IndexingJob.status == JobStatus.QUEUED, _is_stale_running(now)),
        )
        .values(
            status=JobStatus.RUNNING,
            attempts=IndexingJob.attempts + 1,
            started_at=now,
            heartbeat_at=now,
            finished_at=None,
        )
        .returning(IndexingJob.id)
    )
    claimed_id = (await db.execute(stmt)).scalar_one_or_none()
    await db.commit()
    if claimed_id is None:
        return None
    return await get(db, claimed_id)


async def heartbeat(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Report that this job is still being worked on."""
    await db.execute(
        update(IndexingJob)
        .where(IndexingJob.id == job_id, IndexingJob.status == JobStatus.RUNNING)
        .values(heartbeat_at=_now())
    )
    await db.commit()


async def record_progress(db: AsyncSession, job_id: uuid.UUID, *, done: int, total: int) -> None:
    """Checkpoint after a chunk. Doubles as a heartbeat."""
    now = _now()
    await db.execute(
        update(IndexingJob)
        .where(IndexingJob.id == job_id)
        .values(entities_done=done, entities_total=total, heartbeat_at=now)
    )
    await db.commit()


async def mark_succeeded(db: AsyncSession, job_id: uuid.UUID) -> None:
    now = _now()
    await db.execute(
        update(IndexingJob)
        .where(IndexingJob.id == job_id)
        .values(
            status=JobStatus.SUCCEEDED,
            finished_at=now,
            heartbeat_at=None,
            last_error=None,
        )
    )
    await db.commit()


async def mark_failed(
    db: AsyncSession, job_id: uuid.UUID, error: str
) -> tuple[bool, datetime | None]:
    """Record a failure and decide whether it gets another go.

    Returns (will_retry, run_after). On the last attempt the job goes terminal
    and the caller is expected to mark the project failed; before that it goes
    back to queued with `run_after` pushed out by the backoff, and the caller
    re-delivers it to the queue with a matching delay.
    """
    now = _now()
    job = (
        await db.execute(select(IndexingJob).where(IndexingJob.id == job_id))
    ).scalar_one_or_none()
    if job is None:
        return False, None

    will_retry = job.attempts < settings.job_max_attempts
    run_after = now + backoff_delay(job.attempts) if will_retry else job.run_after

    await db.execute(
        update(IndexingJob)
        .where(IndexingJob.id == job_id)
        .values(
            status=JobStatus.QUEUED if will_retry else JobStatus.FAILED,
            last_error=error[: settings.job_error_max_chars],
            run_after=run_after,
            heartbeat_at=None,
            finished_at=None if will_retry else now,
        )
    )
    await db.commit()
    return will_retry, run_after if will_retry else None


async def reconcile(db: AsyncSession) -> list[IndexingJob]:
    """Find work that nothing is going to pick up on its own, and fix it.

    Run at worker startup. Two populations:

    - **Stale running jobs** — a worker was killed mid-run. Nothing will ever
      report on these; the heartbeat going cold is the only signal there is.
    - **Queued jobs with no live delivery** — the API committed the row and then
      failed to reach Redis, or Redis lost the message. The row is the record,
      so the row is what we trust.

    Both are returned for the caller to re-deliver. Jobs already past their
    attempt limit are failed outright instead, along with their project.
    Re-delivering a job that *is* still live is harmless: `claim` will refuse it.
    """
    now = _now()

    rows = (
        await db.execute(
            select(IndexingJob).where(
                or_(IndexingJob.status == JobStatus.QUEUED, _is_stale_running(now))
            )
        )
    ).scalars().all()

    revivable: list[IndexingJob] = []
    for job in rows:
        if job.attempts >= settings.job_max_attempts:
            await db.execute(
                update(IndexingJob)
                .where(IndexingJob.id == job.id)
                .values(
                    status=JobStatus.FAILED,
                    finished_at=now,
                    heartbeat_at=None,
                    last_error=job.last_error
                    or "abandoned: worker stopped responding and no attempts remain",
                )
            )
            await db.execute(
                update(Project)
                .where(Project.id == job.project_id)
                .values(
                    status=ProjectStatus.FAILED,
                    error_message="Indexing was interrupted and could not be recovered.",
                )
            )
            continue

        await db.execute(
            update(IndexingJob)
            .where(IndexingJob.id == job.id)
            .values(status=JobStatus.QUEUED, heartbeat_at=None)
        )
        await db.execute(
            update(Project)
            .where(Project.id == job.project_id)
            .values(status=ProjectStatus.QUEUED)
        )
        revivable.append(job)

    await db.commit()
    return revivable


async def get(db: AsyncSession, job_id: uuid.UUID) -> IndexingJob | None:
    return (
        await db.execute(select(IndexingJob).where(IndexingJob.id == job_id))
    ).scalar_one_or_none()


async def latest_for_project(
    db: AsyncSession, project_id: uuid.UUID
) -> IndexingJob | None:
    return (
        await db.execute(
            select(IndexingJob)
            .where(IndexingJob.project_id == project_id)
            .order_by(IndexingJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def has_active_job(db: AsyncSession, project_id: uuid.UUID) -> bool:
    """Whether a run is already queued or in flight for this project.

    Guards the reindex endpoint: without it, clicking twice queues the same
    expensive work twice.
    """
    row = (
        await db.execute(
            select(IndexingJob.id)
            .where(
                IndexingJob.project_id == project_id,
                IndexingJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None
