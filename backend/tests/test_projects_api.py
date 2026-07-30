"""Project endpoints, with emphasis on cross-user isolation."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.project import ProjectStatus
from app.services import project_service
from tests.conftest import make_project

pytestmark = pytest.mark.db

PROJECTS = "/api/v1/projects/"
REPO = "https://github.com/octocat/Hello-World"


@pytest.fixture(autouse=True)
def no_indexing(monkeypatch):
    """Stop POST /projects from running the real pipeline.

    The pipeline opens its own sessions via session_scope, which bypasses the
    rollback fixture entirely and would leave committed rows behind.
    """
    enqueued: list[str] = []

    async def _fake_pipeline(project_id: str) -> None:
        enqueued.append(project_id)

    from app.api.v1 import projects as projects_module

    monkeypatch.setattr(projects_module, "run_indexing_pipeline", _fake_pipeline)
    return enqueued


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
    async def test_creates_a_pending_project(self, client, auth_headers):
        r = await client.post(PROJECTS, json={"github_repo_url": REPO}, headers=auth_headers)
        assert r.status_code == 201
        assert r.json()["status"] == "pending"

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

    async def test_indexing_is_enqueued(self, client, auth_headers, no_indexing):
        r = await client.post(PROJECTS, json={"github_repo_url": REPO}, headers=auth_headers)
        assert no_indexing == [r.json()["id"]]

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


class TestSearchGuards:
    async def test_searching_an_unindexed_project_is_rejected(self, client, project, auth_headers):
        """status is pending, so this must fail before any paid call is made."""
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
