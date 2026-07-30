"""Delivery of jobs to the worker, over Redis.

Thin on purpose. Everything that has to survive a restart lives in the
`indexing_jobs` table; this module only carries a job id from the API to a
worker. If it fails, the job row still exists as `queued` and the worker's
startup reconciler picks it up — which is why `enqueue` is allowed to swallow
its own errors rather than failing the request that created the project.
"""

import uuid

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import settings

INDEX_TASK = "index_project"

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def get_pool() -> ArqRedis:
    """Process-wide connection pool, opened on first use."""
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue_job(job_id: uuid.UUID, *, delay_seconds: float = 0) -> bool:
    """Ask a worker to run this job. Returns whether Redis accepted it.

    Deliberately does *not* set ARQ's `_job_id`. Deduplicating on it looks
    attractive but breaks retries: ARQ refuses an id that already exists in the
    queue or the result store, so the second attempt at a job would be silently
    dropped. Duplicate delivery is not a problem worth solving here anyway —
    `job_service.claim` is an atomic UPDATE, so a redundant delivery finds the
    job already running and does nothing.

    Never raises. A project whose enqueue failed is not lost: it is `queued` in
    Postgres with no delivery, which is exactly the case `reconcile` repairs.
    """
    try:
        pool = await get_pool()
        job = await pool.enqueue_job(
            INDEX_TASK, str(job_id), _defer_by=delay_seconds or None
        )
        return job is not None
    except Exception as e:
        print(f"[QUEUE] enqueue failed for job={job_id}: {e!r} - left for the reconciler")
        return False
