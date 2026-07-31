"""The indexing worker.

Run with:  arq app.worker.WorkerSettings

Division of labour, because two systems here could each plausibly own retries
and only one should:

- **ARQ/Redis** delivers a job id to this process, and bounds how many run at
  once. That is all. `retry_jobs` and `max_tries` are switched off.
- **Postgres** owns whether a job may run, how many attempts it has had, when
  it may next be tried, and what went wrong. Backoff is computed by
  `job_service` and applied by re-enqueueing with a matching delay.

Letting both retry would multiply the attempt counts together and make the
attempt limit meaningless.
"""

import asyncio
import traceback
import uuid

from arq import cron

from app.config import settings
from app.db.postgres import dispose_engine, session_scope
from app.models.project import ProjectStatus
from app.services import indexing_service, job_service, queue_service


async def _heartbeat_loop(job_id: uuid.UUID) -> None:
    """Report liveness until cancelled.

    This is what distinguishes "still working" from "died holding the job". The
    interval is well inside job_heartbeat_timeout_seconds so an ordinary slow
    write does not read as death.
    """
    while True:
        await asyncio.sleep(settings.job_heartbeat_interval_seconds)
        try:
            async with session_scope() as db:
                await job_service.heartbeat(db, job_id)
        except Exception as e:
            # A failed heartbeat is not fatal on its own — the reconciler will
            # sort it out if they keep failing.
            print(f"[WORKER] heartbeat failed for job={job_id}: {e!r}")


async def index_project(ctx: dict, job_id: str) -> str:
    """Run one indexing job. The only task this worker knows how to do."""
    jid = uuid.UUID(job_id)

    async with session_scope() as db:
        job = await job_service.claim(db, jid)

    if job is None:
        # Already finished, already running elsewhere, not yet due, or out of
        # attempts. All of those are correct outcomes for a duplicate delivery.
        print(f"[WORKER] job={job_id} not claimable, skipping")
        return "skipped"

    project_id = job.project_id
    attempt = job.attempts
    print(f"[WORKER] job={job_id} project={project_id} attempt={attempt} started")

    heart = asyncio.create_task(_heartbeat_loop(jid))
    try:
        async def _progress(done: int, total: int) -> None:
            async with session_scope() as db:
                await job_service.record_progress(db, jid, done=done, total=total)

        result = await indexing_service.run_indexing_pipeline(
            str(project_id), on_progress=_progress
        )

        async with session_scope() as db:
            await job_service.mark_succeeded(db, jid)

        if result is None:
            print(f"[WORKER] job={job_id} project vanished mid-run")
            return "project-gone"

        print(
            f"[WORKER] job={job_id} done: {result.entities_indexed} indexed "
            f"({result.entities_described} described, {result.entities_reused} "
            f"reused, {result.reuse_ratio:.0%} skipped), "
            f"{result.entities_removed} removed, {result.chunks} chunks, "
            f"commit={(result.commit_sha or 'unknown')[:8]}"
        )
        return "ok"

    except Exception as e:
        traceback.print_exc()
        detail = f"{type(e).__name__}: {e}"

        async with session_scope() as db:
            will_retry, run_after = await job_service.mark_failed(db, jid, detail)

        if will_retry:
            delay = job_service.backoff_delay(attempt).total_seconds()
            print(f"[WORKER] job={job_id} failed, retrying in {delay:.0f}s: {detail}")
            await queue_service.enqueue_job(jid, delay_seconds=delay)
            # The project goes back to queued rather than flipping to failed —
            # from a user's point of view the work is still pending, not lost.
            await indexing_service.set_project_status(project_id, ProjectStatus.QUEUED)
        else:
            print(f"[WORKER] job={job_id} failed terminally: {detail}")
            # Generic message on purpose: the detail is in the job row and the
            # logs. git clone stderr can carry a URL with embedded credentials,
            # so it must not be echoed back to the client.
            await indexing_service.set_project_status(
                project_id,
                ProjectStatus.FAILED,
                error_message="Indexing failed. Try re-indexing; if it keeps "
                "failing, check that the repository is public and contains Python.",
            )
        return "failed"

    finally:
        heart.cancel()
        try:
            await heart
        except asyncio.CancelledError:
            pass


async def run_reconciler(reason: str) -> int:
    """Requeue anything nothing else is going to pick up. Returns how many."""
    async with session_scope() as db:
        revived = await job_service.reconcile(db)

    for job in revived:
        await queue_service.enqueue_job(job.id)

    if revived:
        print(f"[WORKER] reconciler ({reason}) requeued {len(revived)} job(s)")
    else:
        print(f"[WORKER] reconciler ({reason}) found nothing to recover")
    return len(revived)


async def reconcile_tick(ctx: dict) -> int:
    """Periodic sweep.

    Startup alone is not enough. A worker killed and restarted quickly leaves a
    job whose heartbeat is not yet stale, so the startup pass correctly skips
    it — and with no later pass, nothing would ever look at it again. That is
    the same stranded-job bug in a narrower window, so the sweep has to repeat.
    """
    return await run_reconciler("periodic")


async def startup(ctx: dict) -> None:
    """Recover anything the last worker left behind, then start consuming.

    This is the fix for the stranded-project bug: before this existed, killing
    the process mid-index left the project in `indexing` with nothing that would
    ever look at it again.
    """
    await run_reconciler("startup")


async def shutdown(ctx: dict) -> None:
    await queue_service.close_pool()
    await dispose_engine()


class WorkerSettings:
    functions = [index_project]

    # Twice a minute. ARQ runs cron jobs on one worker at a time, and reconcile
    # is idempotent anyway — a job it requeues twice is still claimed once.
    cron_jobs = [cron(reconcile_tick, second={0, 30}, run_at_startup=False)]

    on_startup = startup
    on_shutdown = shutdown

    # Bounds simultaneous clones, and through them in-flight LLM calls: ten
    # users adding repos at once queues rather than launching ten clones and
    # ~100 concurrent OpenAI requests.
    max_jobs = settings.worker_concurrency

    # Retries belong to job_service. See the module docstring.
    max_tries = 1
    retry_jobs = False

    # A large repo legitimately takes a long time; this is the ceiling on any
    # single attempt, after which ARQ cancels it and the heartbeat goes cold.
    job_timeout = 3600

    redis_settings = queue_service.redis_settings()
