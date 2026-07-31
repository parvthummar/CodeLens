"""Job lifecycle: claiming, retrying, and recovering from a dead worker.

These are the guarantees the worker rests on, and they are all enforced in SQL
rather than in the worker process — which is the point, since the failure mode
being defended against is the worker process not existing any more.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.indexing_job import IndexingJob, JobStatus
from app.models.project import ProjectStatus
from app.services import job_service
from tests.conftest import make_project

pytestmark = pytest.mark.db


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def make_job(db, project, **overrides) -> IndexingJob:
    fields = {
        "id": uuid.uuid4(),
        "project_id": project.id,
        "status": JobStatus.QUEUED,
        "run_after": _now(),
        "attempts": 0,
    }
    fields.update(overrides)
    job = IndexingJob(**fields)
    db.add(job)
    await db.flush()
    return job


async def reload(db, instance):
    """Re-read a row the service updated behind the session's back.

    The services issue UPDATE statements, not ORM mutations, so the identity map
    still holds pre-update values. `refresh` must be awaited — expiring and then
    touching an attribute would emit lazy IO from a sync context and raise
    MissingGreenlet.
    """
    await db.refresh(instance)
    return instance


class TestCreate:
    async def test_starts_queued_and_due(self, db, project):
        job = await job_service.create_job(db, project.id)
        await db.flush()
        assert job.status == JobStatus.QUEUED
        assert job.attempts == 0
        assert job.run_after <= _now()


class TestClaim:
    async def test_claims_a_due_queued_job(self, db, project):
        job = await make_job(db, project)
        claimed = await job_service.claim(db, job.id)
        assert claimed is not None
        assert claimed.status == JobStatus.RUNNING

    async def test_claiming_burns_an_attempt(self, db, project):
        """Counted on claim, not on failure: a job that kills its worker
        outright never reports a failure, and must still exhaust its budget."""
        job = await make_job(db, project)
        claimed = await job_service.claim(db, job.id)
        assert claimed.attempts == 1

    async def test_sets_a_heartbeat_immediately(self, db, project):
        job = await make_job(db, project)
        claimed = await job_service.claim(db, job.id)
        assert claimed.heartbeat_at is not None

    async def test_a_second_worker_gets_nothing(self, db, project):
        """The no-double-processing guarantee, in one assertion."""
        job = await make_job(db, project)
        first = await job_service.claim(db, job.id)
        second = await job_service.claim(db, job.id)
        assert first is not None
        assert second is None

    async def test_not_claimable_before_run_after(self, db, project):
        job = await make_job(db, project, run_after=_now() + timedelta(minutes=5))
        assert await job_service.claim(db, job.id) is None

    async def test_succeeded_jobs_are_not_reclaimable(self, db, project):
        job = await make_job(db, project, status=JobStatus.SUCCEEDED)
        assert await job_service.claim(db, job.id) is None

    async def test_failed_jobs_are_not_reclaimable(self, db, project):
        job = await make_job(db, project, status=JobStatus.FAILED)
        assert await job_service.claim(db, job.id) is None

    async def test_unknown_job_is_not_claimable(self, db):
        assert await job_service.claim(db, uuid.uuid4()) is None

    async def test_a_running_job_with_a_stale_heartbeat_is_reclaimable(self, db, project):
        """This is what makes a killed worker recoverable rather than terminal."""
        stale = _now() - timedelta(
            seconds=settings.job_heartbeat_timeout_seconds + 60
        )
        job = await make_job(
            db, project, status=JobStatus.RUNNING, heartbeat_at=stale, attempts=1
        )
        claimed = await job_service.claim(db, job.id)
        assert claimed is not None
        assert claimed.attempts == 2

    async def test_a_running_job_with_no_heartbeat_is_reclaimable(self, db, project):
        """NULL must read as stale, or such a row is stranded forever."""
        job = await make_job(
            db, project, status=JobStatus.RUNNING, heartbeat_at=None, attempts=1
        )
        assert await job_service.claim(db, job.id) is not None


class TestHeartbeat:
    async def test_moves_the_timestamp_forward(self, db, project, monkeypatch):
        job = await make_job(db, project)
        claimed = await job_service.claim(db, job.id)
        before = claimed.heartbeat_at

        # The clock is pinned rather than trusted. claim() and heartbeat() both
        # stamp _now(), and against a local Postgres the two round trips can
        # complete inside one tick of a coarse system clock — which made a
        # strict `>` fail intermittently. The behaviour under test is that
        # heartbeat writes the current time, not that Postgres is slow.
        later = before + timedelta(seconds=30)
        monkeypatch.setattr(job_service, "_now", lambda: later)

        await job_service.heartbeat(db, job.id)
        assert (await reload(db, job)).heartbeat_at == later

    async def test_keeps_a_long_run_out_of_the_reconciler(self, db, project):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)
        await job_service.heartbeat(db, job.id)

        assert [j.id for j in await job_service.reconcile(db)] == []

    async def test_does_not_resurrect_a_finished_job(self, db, project):
        job = await make_job(db, project, status=JobStatus.SUCCEEDED)
        await job_service.heartbeat(db, job.id)
        assert (await reload(db, job)).heartbeat_at is None


class TestProgress:
    async def test_records_the_checkpoint(self, db, project):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)

        await job_service.record_progress(db, job.id, done=200, total=560)

        fresh = await reload(db, job)
        assert (fresh.entities_done, fresh.entities_total) == (200, 560)

    async def test_doubles_as_a_heartbeat(self, db, project):
        """A chunk can take longer than the heartbeat interval; progress must
        count as liveness or a working job looks dead."""
        job = await make_job(db, project, status=JobStatus.RUNNING, heartbeat_at=None)
        await job_service.record_progress(db, job.id, done=1, total=2)
        assert (await reload(db, job)).heartbeat_at is not None


class TestBackoff:
    def test_grows_exponentially(self):
        delays = [job_service.backoff_delay(n).total_seconds() for n in (1, 2, 3)]
        base = settings.job_backoff_base_seconds
        assert delays == [base, base * 2, base * 4]

    def test_is_capped(self):
        assert (
            job_service.backoff_delay(50).total_seconds()
            == settings.job_backoff_max_seconds
        )


class TestFailure:
    async def test_first_failure_is_retried(self, db, project):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)

        will_retry, run_after = await job_service.mark_failed(db, job.id, "boom")

        assert will_retry is True
        assert run_after > _now()
        assert (await reload(db, job)).status == JobStatus.QUEUED

    async def test_retry_is_deferred_by_the_backoff(self, db, project):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)
        await job_service.mark_failed(db, job.id, "boom")

        fresh = await reload(db, job)
        assert fresh.run_after > _now() + timedelta(
            seconds=settings.job_backoff_base_seconds - 5
        )

    async def test_a_deferred_retry_is_not_immediately_claimable(self, db, project):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)
        await job_service.mark_failed(db, job.id, "boom")

        assert await job_service.claim(db, job.id) is None

    async def test_the_error_is_recorded(self, db, project):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)
        await job_service.mark_failed(db, job.id, "RuntimeError: rate limited")
        assert "rate limited" in (await reload(db, job)).last_error

    async def test_a_huge_error_is_truncated(self, db, project):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)
        await job_service.mark_failed(db, job.id, "x" * 50_000)
        assert len((await reload(db, job)).last_error) == settings.job_error_max_chars

    async def test_goes_terminal_on_the_last_attempt(self, db, project):
        job = await make_job(db, project, attempts=settings.job_max_attempts - 1)
        await job_service.claim(db, job.id)  # -> attempts == job_max_attempts

        will_retry, run_after = await job_service.mark_failed(db, job.id, "boom")

        assert will_retry is False
        assert run_after is None
        fresh = await reload(db, job)
        assert fresh.status == JobStatus.FAILED
        assert fresh.finished_at is not None

    async def test_exactly_max_attempts_are_made(self, db, project):
        """One try plus the configured retries, and then it stops."""
        job = await make_job(db, project)
        attempts = 0
        while await job_service.claim(db, job.id) is not None:
            attempts += 1
            await job_service.mark_failed(db, job.id, "boom")
            # Undo the backoff so the loop does not have to wait it out.
            fresh = await job_service.get(db, job.id)
            fresh.run_after = _now()
            await db.flush()

        assert attempts == settings.job_max_attempts

    async def test_unknown_job_does_not_raise(self, db):
        assert await job_service.mark_failed(db, uuid.uuid4(), "boom") == (False, None)


class TestSuccess:
    async def test_marks_succeeded_and_clears_the_heartbeat(self, db, project):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)
        await job_service.mark_succeeded(db, job.id)

        fresh = await reload(db, job)
        assert fresh.status == JobStatus.SUCCEEDED
        assert fresh.heartbeat_at is None
        assert fresh.finished_at is not None

    async def test_clears_an_error_from_an_earlier_attempt(self, db, project):
        job = await make_job(db, project, last_error="previous failure")
        await job_service.claim(db, job.id)
        await job_service.mark_succeeded(db, job.id)
        assert (await reload(db, job)).last_error is None


class TestReconcile:
    async def test_requeues_a_job_whose_worker_died(self, db, project):
        """The specific bug this whole table exists for: before the worker, a
        killed process left the project in `indexing` forever."""
        stale = _now() - timedelta(seconds=settings.job_heartbeat_timeout_seconds + 60)
        job = await make_job(
            db, project, status=JobStatus.RUNNING, heartbeat_at=stale, attempts=1
        )
        project.status = ProjectStatus.INDEXING
        await db.flush()

        revived = await job_service.reconcile(db)

        assert [j.id for j in revived] == [job.id]
        assert (await reload(db, job)).status == JobStatus.QUEUED

    async def test_resets_the_project_to_queued(self, db, project):
        stale = _now() - timedelta(seconds=settings.job_heartbeat_timeout_seconds + 60)
        await make_job(db, project, status=JobStatus.RUNNING, heartbeat_at=stale)
        project.status = ProjectStatus.INDEXING
        await db.flush()

        await job_service.reconcile(db)

        await reload(db, project)
        assert project.status == ProjectStatus.QUEUED

    async def test_recovers_a_job_that_never_reached_redis(self, db, project):
        """Enqueue can fail after the row commits. The row is the record."""
        job = await make_job(db, project, status=JobStatus.QUEUED)
        assert [j.id for j in await job_service.reconcile(db)] == [job.id]

    async def test_leaves_a_live_run_alone(self, db, project):
        job = await make_job(db, project)
        await job_service.claim(db, job.id)  # fresh heartbeat

        assert await job_service.reconcile(db) == []
        assert (await reload(db, job)).status == JobStatus.RUNNING

    async def test_leaves_finished_jobs_alone(self, db, project):
        await make_job(db, project, status=JobStatus.SUCCEEDED)
        await make_job(db, project, status=JobStatus.FAILED)
        assert await job_service.reconcile(db) == []

    async def test_gives_up_on_a_job_with_no_attempts_left(self, db, project):
        stale = _now() - timedelta(seconds=settings.job_heartbeat_timeout_seconds + 60)
        job = await make_job(
            db,
            project,
            status=JobStatus.RUNNING,
            heartbeat_at=stale,
            attempts=settings.job_max_attempts,
        )

        assert await job_service.reconcile(db) == []
        assert (await reload(db, job)).status == JobStatus.FAILED

    async def test_an_abandoned_job_fails_its_project(self, db, project):
        stale = _now() - timedelta(seconds=settings.job_heartbeat_timeout_seconds + 60)
        await make_job(
            db,
            project,
            status=JobStatus.RUNNING,
            heartbeat_at=stale,
            attempts=settings.job_max_attempts,
        )
        project.status = ProjectStatus.INDEXING
        await db.flush()

        await job_service.reconcile(db)

        await reload(db, project)
        assert project.status == ProjectStatus.FAILED
        assert project.error_message is not None

    async def test_records_why_it_was_abandoned(self, db, project):
        stale = _now() - timedelta(seconds=settings.job_heartbeat_timeout_seconds + 60)
        job = await make_job(
            db,
            project,
            status=JobStatus.RUNNING,
            heartbeat_at=stale,
            attempts=settings.job_max_attempts,
        )
        await job_service.reconcile(db)
        assert "abandoned" in (await reload(db, job)).last_error


class TestActiveJobGuard:
    async def test_true_while_queued(self, db, project):
        await make_job(db, project, status=JobStatus.QUEUED)
        assert await job_service.has_active_job(db, project.id) is True

    async def test_true_while_running(self, db, project):
        await make_job(db, project, status=JobStatus.RUNNING)
        assert await job_service.has_active_job(db, project.id) is True

    async def test_false_once_finished(self, db, project):
        await make_job(db, project, status=JobStatus.SUCCEEDED)
        await make_job(db, project, status=JobStatus.FAILED)
        assert await job_service.has_active_job(db, project.id) is False

    async def test_scoped_to_one_project(self, db, user, project):
        other = make_project(user)
        db.add(other)
        await db.flush()
        await make_job(db, other, status=JobStatus.QUEUED)

        assert await job_service.has_active_job(db, project.id) is False


class TestCascade:
    async def test_jobs_go_when_the_project_goes(self, db, project):
        job = await make_job(db, project)
        await db.delete(project)
        await db.flush()
        assert await job_service.get(db, job.id) is None
