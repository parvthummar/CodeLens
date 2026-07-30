"""Project endpoints, with emphasis on cross-user isolation."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.project import ProjectStatus
from app.services import job_service, project_service, queue_service
from tests.conftest import make_project

pytestmark = pytest.mark.db

PROJECTS = "/api/v1/projects/"
REPO = "https://github.com/octocat/Hello-World"


@pytest.fixture(autouse=True)
def enqueued(monkeypatch):
    """Capture queue deliveries instead of reaching Redis.

    The API no longer runs the pipeline — it writes a job row and hands the id
    to Redis. Only the second half needs stubbing; the job row is written in the
    request's own transaction and is rolled back with everything else.
    """
    delivered: list[tuple[str, float]] = []

    async def _fake_enqueue(job_id, *, delay_seconds: float = 0) -> bool:
        delivered.append((str(job_id), delay_seconds))
        return True

    monkeypatch.setattr(queue_service, "enqueue_job", _fake_enqueue)
    return delivered


@pytest.fixture(autouse=True)
def fake_pinecone_delete(monkeypatch):
    """Record namespace deletions instead of calling Pinecone."""
    deleted: list[str] = []

    async def _fake_delete(namespace: str) -> None:
        deleted.append(namespace)

    monkeypatch.setattr(project_service, "delete_namespace", _fake_delete)
    return deleted


def headers_for(user) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token({'sub': user.email})}"}


class TestCreate:
    async def test_creates_a_queued_project(self, client, auth_headers):
        r = await client.post(PROJECTS, json={"github_repo_url": REPO}, headers=auth_headers)
        assert r.status_code == 201
        assert r.json()["status"] == "queued"

    async def test_parses_owner_and_repo_from_the_url(self, client, auth_headers):
        r = await client.post(PROJECTS, json={"github_repo_url": REPO}, headers=auth_headers)
        assert r.json()["github_owner"] == "octocat"
        assert r.json()["github_repo_name"] == "Hello-World"

    async def test_name_defaults_to_the_repo_name(self, client, auth_headers):
        r = await client.post(PROJECTS, json={"github_repo_url": REPO}, headers=auth_headers)
        assert r.json()["name"] == "Hello-World"

    async def test_explicit_name_is_kept(self, client, auth_headers):
        r = await client.post(
            PROJECTS, json={"github_repo_url": REPO, "name": "My Repo"}, headers=auth_headers
        )
        assert r.json()["name"] == "My Repo"

    async def test_trailing_git_suffix_is_stripped(self, client, auth_headers):
        r = await client.post(
            PROJECTS, json={"github_repo_url": REPO + ".git"}, headers=auth_headers
        )
        assert r.json()["github_repo_name"] == "Hello-World"

    async def test_a_job_row_is_written_for_the_project(self, client, auth_headers, db):
        r = await client.post(PROJECTS, json={"github_repo_url": REPO}, headers=auth_headers)

        job = await job_service.latest_for_project(db, uuid.UUID(r.json()["id"]))
        assert job is not None
        assert job.status.value == "queued"
        assert job.attempts == 0

    async def test_the_job_is_handed_to_the_queue(self, client, auth_headers, db, enqueued):
        r = await client.post(PROJECTS, json={"github_repo_url": REPO}, headers=auth_headers)

        job = await job_service.latest_for_project(db, uuid.UUID(r.json()["id"]))
        assert enqueued == [(str(job.id), 0)]

    async def test_a_failed_enqueue_still_creates_the_project(
        self, client, auth_headers, db, monkeypatch
    ):
        """Redis being down must not fail the request — the row is the record."""
        async def _down(job_id, *, delay_seconds: float = 0) -> bool:
            return False

        monkeypatch.setattr(queue_service, "enqueue_job", _down)

        r = await client.post(PROJECTS, json={"github_repo_url": REPO}, headers=auth_headers)
        assert r.status_code == 201

        # Still queued in Postgres, which is what the reconciler looks for.
        job = await job_service.latest_for_project(db, uuid.UUID(r.json()["id"]))
        assert job.status.value == "queued"

    async def test_url_without_owner_and_repo_is_rejected(self, client, auth_headers):
        r = await client.post(
            PROJECTS, json={"github_repo_url": "https://github.com/"}, headers=auth_headers
        )
        assert r.status_code == 400

    async def test_requires_authentication(self, client):
        assert (await client.post(PROJECTS, json={"github_repo_url": REPO})).status_code == 401

    async def test_namespace_matches_the_id(self, client, db, auth_headers):
        r = await client.post(PROJECTS, json={"github_repo_url": REPO}, headers=auth_headers)
        from app.models.project import Project

        stored = await db.get(Project, uuid.UUID(r.json()["id"]))
        assert stored.pinecone_namespace == r.json()["id"]


class TestList:
    async def test_empty_by_default(self, client, auth_headers):
        assert (await client.get(PROJECTS, headers=auth_headers)).json() == []

    async def test_returns_own_projects(self, client, db, user, auth_headers):
        db.add(make_project(user))
        await db.flush()
        assert len((await client.get(PROJECTS, headers=auth_headers)).json()) == 1

    async def test_does_not_leak_other_users_projects(self, client, db, user, other_user, auth_headers):
        db.add(make_project(user, name="mine"))
        db.add(make_project(other_user, name="theirs"))
        await db.flush()
        names = [p["name"] for p in (await client.get(PROJECTS, headers=auth_headers)).json()]
        assert names == ["mine"]

    async def test_newest_first(self, client, db, user, auth_headers):
        now = datetime.now(timezone.utc)
        db.add(make_project(user, name="older", created_at=now - timedelta(hours=1)))
        db.add(make_project(user, name="newer", created_at=now))
        await db.flush()
        names = [p["name"] for p in (await client.get(PROJECTS, headers=auth_headers)).json()]
        assert names == ["newer", "older"]


class TestGet:
    async def test_returns_own_project(self, client, project, auth_headers):
        r = await client.get(f"{PROJECTS}{project.id}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["id"] == str(project.id)

    async def test_other_users_project_is_not_found(self, client, db, other_user, auth_headers):
        """404 rather than 403, so existence is not disclosed."""
        theirs = make_project(other_user)
        db.add(theirs)
        await db.flush()
        assert (await client.get(f"{PROJECTS}{theirs.id}", headers=auth_headers)).status_code == 404

    async def test_malformed_id_is_a_bad_request(self, client, auth_headers):
        assert (await client.get(f"{PROJECTS}not-a-uuid", headers=auth_headers)).status_code == 400

    async def test_unknown_id_is_not_found(self, client, auth_headers):
        assert (await client.get(f"{PROJECTS}{uuid.uuid4()}", headers=auth_headers)).status_code == 404

    async def test_exposes_the_fields_the_frontend_reads(self, client, project, auth_headers):
        body = (await client.get(f"{PROJECTS}{project.id}", headers=auth_headers)).json()
        assert {
            "id", "name", "github_repo_url", "status",
            "error_message", "created_at", "updated_at",
        } <= set(body)


class TestDelete:
    async def test_removes_the_project(self, client, project, auth_headers):
        assert (await client.delete(f"{PROJECTS}{project.id}", headers=auth_headers)).status_code == 204
        assert (await client.get(f"{PROJECTS}{project.id}", headers=auth_headers)).status_code == 404

    async def test_cleans_up_the_pinecone_namespace(self, client, project, auth_headers, fake_pinecone_delete):
        await client.delete(f"{PROJECTS}{project.id}", headers=auth_headers)
        assert fake_pinecone_delete == [str(project.id)]

    async def test_cannot_delete_another_users_project(self, client, db, other_user, auth_headers):
        theirs = make_project(other_user)
        db.add(theirs)
        await db.flush()
        assert (await client.delete(f"{PROJECTS}{theirs.id}", headers=auth_headers)).status_code == 404
        # And it is still there afterwards.
        assert (await client.get(f"{PROJECTS}{theirs.id}", headers=headers_for(other_user))).status_code == 200

    async def test_requires_authentication(self, client, project):
        assert (await client.delete(f"{PROJECTS}{project.id}")).status_code == 401


class TestReindex:
    async def test_queues_a_new_job(self, client, db, user, auth_headers, enqueued):
        record = make_project(user, status=ProjectStatus.READY)
        db.add(record)
        await db.flush()

        r = await client.post(f"{PROJECTS}{record.id}/reindex", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "queued"

        job = await job_service.latest_for_project(db, record.id)
        assert job is not None
        assert enqueued == [(str(job.id), 0)]

    async def test_clears_a_previous_error(self, client, db, user, auth_headers):
        record = make_project(user, status=ProjectStatus.FAILED, error_message="boom")
        db.add(record)
        await db.flush()

        r = await client.post(f"{PROJECTS}{record.id}/reindex", headers=auth_headers)
        assert r.json()["error_message"] is None

    async def test_refuses_while_a_run_is_already_queued(
        self, client, db, user, auth_headers, enqueued
    ):
        """Re-indexing is the most expensive operation here; don't pay twice."""
        record = make_project(user, status=ProjectStatus.READY)
        db.add(record)
        await db.flush()

        assert (await client.post(f"{PROJECTS}{record.id}/reindex", headers=auth_headers)).status_code == 200
        second = await client.post(f"{PROJECTS}{record.id}/reindex", headers=auth_headers)

        assert second.status_code == 409
        assert len(enqueued) == 1

    async def test_cannot_reindex_another_users_project(
        self, client, db, other_user, auth_headers
    ):
        theirs = make_project(other_user, status=ProjectStatus.READY)
        db.add(theirs)
        await db.flush()
        r = await client.post(f"{PROJECTS}{theirs.id}/reindex", headers=auth_headers)
        assert r.status_code == 404

    async def test_requires_authentication(self, client, project):
        assert (await client.post(f"{PROJECTS}{project.id}/reindex")).status_code == 401


class TestSearchGuards:
    async def test_searching_an_unindexed_project_is_rejected(self, client, project, auth_headers):
        """status is queued, so this must fail before any paid call is made."""
        r = await client.post(
            f"{PROJECTS}{project.id}/search", json={"query": "anything"}, headers=auth_headers
        )
        assert r.status_code == 400
        assert "not ready" in r.json()["detail"].lower()

    @pytest.mark.parametrize("status", [ProjectStatus.CLONING, ProjectStatus.INDEXING, ProjectStatus.FAILED])
    async def test_rejected_for_every_non_ready_status(self, client, db, user, auth_headers, status):
        record = make_project(user, status=status)
        db.add(record)
        await db.flush()
        r = await client.post(
            f"{PROJECTS}{record.id}/search", json={"query": "anything"}, headers=auth_headers
        )
        assert r.status_code == 400

    async def test_cannot_search_another_users_project(self, client, db, other_user, auth_headers):
        theirs = make_project(other_user, status=ProjectStatus.READY)
        db.add(theirs)
        await db.flush()
        r = await client.post(
            f"{PROJECTS}{theirs.id}/search", json={"query": "anything"}, headers=auth_headers
        )
        assert r.status_code == 404

    async def test_requires_authentication(self, client, project):
        r = await client.post(f"{PROJECTS}{project.id}/search", json={"query": "x"})
        assert r.status_code == 401
