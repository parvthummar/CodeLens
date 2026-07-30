"""The worker task: claim, run, and decide what a failure means.

The pipeline itself is stubbed here — it has its own tests. What matters at this
level is the orchestration around it, which is the part that determines whether
a project can get stranded.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app import worker
from app.config import settings
from app.models.indexing_job import JobStatus
from app.models.project import ProjectStatus
from app.services import indexing_service, job_service, queue_service
from tests.test_job_service import make_job, reload

pytestmark = pytest.mark.db


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def wired(db, monkeypatch):
    """Point the worker at the rollback session and record its side effects."""
    calls = {"ran": [], "enqueued": [], "progress": []}
    behaviour = {"raise": None, "result": indexing_service.IndexingResult(3, 0, 1)}

    @asynccontextmanager
    async def _scope():
        yield db

    async def _pipeline(project_id, *, on_progress=None):
        calls["ran"].append(project_id)
        if on_progress:
            await on_progress(1, 1)
        if behaviour["raise"] is not None:
            raise behaviour["raise"]
        return behaviour["result"]

    async def _enqueue(job_id, *, delay_seconds: float = 0) -> bool:
        calls["enqueued"].append((str(job_id), delay_seconds))
        return True

    monkeypatch.setattr(worker, "session_scope", _scope)
    monkeypatch.setattr(indexing_service, "session_scope", _scope)
    monkeypatch.setattr(worker.indexing_service, "run_indexing_pipeline", _pipeline)
    monkeypatch.setattr(queue_service, "enqueue_job", _enqueue)

    # The heartbeat loop is a background task on a timer; nothing here runs long
    # enough to need it, and leaving it out keeps the tests deterministic.
    async def _no_heartbeat(job_id):
        return None

    monkeypatch.setattr(worker, "_heartbeat_loop", _no_heartbeat)

    calls["behaviour"] = behaviour
    return calls


class TestHappyPath:
    async def test_runs_the_pipeline_for_the_claimed_job(self, db, project, wired):
        job = await make_job(db, project)
        assert await worker.index_project({}, str(job.id)) == "ok"
        assert wired["ran"] == [str(project.id)]

    async def test_marks_the_job_succeeded(self, db, project, wired):
        job = await make_job(db, project)
        await worker.index_project({}, str(job.id))
        assert (await reload(db, job)).status == JobStatus.SUCCEEDED

    async def test_records_progress_from_the_pipeline(self, db, project, wired):
        job = await make_job(db, project)
        await worker.index_project({}, str(job.id))
        assert (await reload(db, job)).entities_done == 1

    async def test_does_not_requeue_on_success(self, db, project, wired):
        job = await make_job(db, project)
        await worker.index_project({}, str(job.id))
        assert wired["enqueued"] == []


class TestDuplicateDelivery:
    async def test_a_second_delivery_does_nothing(self, db, project, wired):
        """Redis can deliver twice; the DB claim is what makes that safe."""
        job = await make_job(db, project)
        await worker.index_project({}, str(job.id))

        assert await worker.index_project({}, str(job.id)) == "skipped"
        assert wired["ran"] == [str(project.id)]

    async def test_a_delivery_for_a_running_job_does_nothing(self, db, project, wired):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)  # another worker has it, heartbeat fresh

        assert await worker.index_project({}, str(job.id)) == "skipped"
        assert wired["ran"] == []

    async def test_an_unknown_job_is_skipped_not_crashed(self, db, wired):
        assert await worker.index_project({}, str(uuid.uuid4())) == "skipped"


class TestRetry:
    async def test_a_failure_requeues_with_backoff(self, db, project, wired):
        job = await make_job(db, project)
        wired["behaviour"]["raise"] = RuntimeError("rate limited")

        assert await worker.index_project({}, str(job.id)) == "failed"

        (delivered_id, delay), = wired["enqueued"]
        assert delivered_id == str(job.id)
        assert delay == settings.job_backoff_base_seconds

    async def test_the_project_stays_queued_while_retries_remain(self, db, project, wired):
        """A retryable failure is not something to alarm the user about."""
        job = await make_job(db, project)
        wired["behaviour"]["raise"] = RuntimeError("rate limited")

        await worker.index_project({}, str(job.id))

        await reload(db, project)
        assert project.status == ProjectStatus.QUEUED

    async def test_the_error_is_kept_on_the_job(self, db, project, wired):
        job = await make_job(db, project)
        wired["behaviour"]["raise"] = RuntimeError("rate limited")
        await worker.index_project({}, str(job.id))
        assert "rate limited" in (await reload(db, job)).last_error

    async def test_the_last_attempt_fails_the_project(self, db, project, wired):
        job = await make_job(db, project, attempts=settings.job_max_attempts - 1)
        wired["behaviour"]["raise"] = RuntimeError("still broken")

        await worker.index_project({}, str(job.id))

        assert (await reload(db, job)).status == JobStatus.FAILED
        await reload(db, project)
        assert project.status == ProjectStatus.FAILED

    async def test_no_further_delivery_once_terminal(self, db, project, wired):
        job = await make_job(db, project, attempts=settings.job_max_attempts - 1)
        wired["behaviour"]["raise"] = RuntimeError("still broken")
        await worker.index_project({}, str(job.id))
        assert wired["enqueued"] == []

    async def test_the_client_facing_message_hides_internals(self, db, project, wired):
        """git clone stderr can carry a URL with embedded credentials, so the
        raw exception must not become the user-visible error."""
        job = await make_job(db, project, attempts=settings.job_max_attempts - 1)
        wired["behaviour"]["raise"] = RuntimeError(
            "fatal: could not read Username for 'https://user:hunter2@github.com'"
        )

        await worker.index_project({}, str(job.id))

        await reload(db, project)
        assert "hunter2" not in project.error_message
        assert "hunter2" in (await reload(db, job)).last_error

    async def test_a_retry_succeeds_and_clears_the_error(self, db, project, wired):
        job = await make_job(db, project)
        wired["behaviour"]["raise"] = RuntimeError("transient")
        await worker.index_project({}, str(job.id))

        # The backoff would otherwise make this unclaimable.
        fresh = await job_service.get(db, job.id)
        fresh.run_after = _now()
        await db.flush()

        wired["behaviour"]["raise"] = None
        assert await worker.index_project({}, str(job.id)) == "ok"

        assert (await reload(db, job)).status == JobStatus.SUCCEEDED
        await reload(db, project)
        assert project.error_message is None


class TestDeletedProject:
    async def test_a_project_deleted_mid_run_is_not_a_failure(self, db, project, wired):
        job = await make_job(db, project)
        wired["behaviour"]["result"] = None

        assert await worker.index_project({}, str(job.id)) == "project-gone"
        assert (await reload(db, job)).status == JobStatus.SUCCEEDED


class TestStartupReconciler:
    async def test_redelivers_a_job_stranded_by_a_dead_worker(self, db, project, wired):
        """The measured bug, closed: killing the process used to leave the
        project in `indexing` with nothing that would ever look at it again."""
        stale = _now() - timedelta(seconds=settings.job_heartbeat_timeout_seconds + 60)
        job = await make_job(
            db, project, status=JobStatus.RUNNING, heartbeat_at=stale, attempts=1
        )
        project.status = ProjectStatus.INDEXING
        await db.flush()

        await worker.startup({})

        assert wired["enqueued"] == [(str(job.id), 0)]
        assert (await reload(db, job)).status == JobStatus.QUEUED

    async def test_redelivers_a_job_that_never_reached_redis(self, db, project, wired):
        job = await make_job(db, project, status=JobStatus.QUEUED)
        await worker.startup({})
        assert wired["enqueued"] == [(str(job.id), 0)]

    async def test_quiet_when_there_is_nothing_to_recover(self, db, wired):
        await worker.startup({})
        assert wired["enqueued"] == []

    async def test_the_periodic_sweep_catches_a_fast_restart(self, db, project, wired):
        """Startup alone leaves a window: restart quickly enough and the dead
        worker's heartbeat is not yet stale, so the startup pass skips the job
        and nothing looks at it again. The sweep has to repeat."""
        job = await make_job(db, project, status=JobStatus.RUNNING, heartbeat_at=_now())
        await worker.startup({})
        assert wired["enqueued"] == []  # correctly left alone: still looks live

        # Time passes; the heartbeat goes cold with no worker to refresh it.
        fresh = await job_service.get(db, job.id)
        fresh.heartbeat_at = _now() - timedelta(
            seconds=settings.job_heartbeat_timeout_seconds + 60
        )
        await db.flush()

        assert await worker.reconcile_tick({}) == 1
        assert wired["enqueued"] == [(str(job.id), 0)]
