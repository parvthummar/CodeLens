"""The indexing pipeline, with the network stubbed but the real parser running.

clone_repo writes actual .py files into the destination, so parse_codebase,
dedupe, and the Postgres writes all execute for real. Only the paid or networked
calls are replaced.
"""

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text

from app.services import (
    embedding_service,
    entity_service,
    github_service,
    indexing_service,
    llm_service,
    pinecone_service,
)

pytestmark = pytest.mark.db

SOURCE = "def alpha():\n    return 1\n\n\nclass Thing:\n    def go(self):\n        pass\n"
# alpha, Thing, Thing.go
EXPECTED_ENTITIES = 3


@pytest.fixture
def pipeline(db, monkeypatch):
    """Wire the pipeline to the test session and record its external effects."""
    calls = {
        "cloned": [],
        "described": [],
        "embedded": [],
        "upserted": [],
        "deleted_vectors": [],
        "statuses": [],
        "dest_dirs": [],
    }
    files = {"m.py": SOURCE}

    @asynccontextmanager
    async def _scope():
        # Yield the rollback-bound session so pipeline writes are undone. Its
        # commit() releases a savepoint rather than committing for real.
        yield db

    async def _clone(url, dest):
        calls["cloned"].append(url)
        calls["dest_dirs"].append(dest)
        for name, body in files.items():
            path = Path(dest) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    async def _describe(entities, batch_size=10):
        calls["described"].append([e.name for e in entities])
        return [f"description of {e.name}" for e in entities]

    async def _embed(texts):
        calls["embedded"].append(list(texts))
        return [[0.05] * 1024 for _ in texts]

    async def _upsert(namespace, vectors):
        calls["upserted"].append((namespace, list(vectors)))

    async def _delete_vectors(namespace, ids):
        calls["deleted_vectors"].append((namespace, list(ids)))

    original_set_status = indexing_service._set_status

    async def _spy_set_status(project_id, status, *, error_message=None):
        calls["statuses"].append(status.value)
        await original_set_status(project_id, status, error_message=error_message)

    monkeypatch.setattr(indexing_service, "session_scope", _scope)
    monkeypatch.setattr(github_service, "clone_repo", _clone)
    monkeypatch.setattr(llm_service, "generate_descriptions_batch", _describe)
    monkeypatch.setattr(embedding_service, "embed_texts", _embed)
    monkeypatch.setattr(pinecone_service, "upsert_vectors", _upsert)
    monkeypatch.setattr(pinecone_service, "delete_vectors", _delete_vectors)
    monkeypatch.setattr(indexing_service, "_set_status", _spy_set_status)

    calls["files"] = files
    return calls


async def status_of(db, project_id) -> tuple[str, str | None]:
    row = (
        await db.execute(
            text("SELECT status, error_message FROM projects WHERE id = :i"), {"i": project_id}
        )
    ).one()
    return row[0], row[1]


class TestHappyPath:
    async def test_reaches_ready(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        status, error = await status_of(db, project.id)
        assert status == "ready"
        assert error is None

    async def test_moves_through_every_status(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert pipeline["statuses"] == ["cloning", "indexing", "ready"]

    async def test_clones_the_configured_repo(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert pipeline["cloned"] == [project.github_repo_url]

    async def test_persists_every_parsed_entity(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert await entity_service.count_for_project(db, project.id) == EXPECTED_ENTITIES

    async def test_describes_functions_classes_and_methods(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert set(pipeline["described"][0]) == {"alpha", "Thing", "Thing.go"}

    async def test_embeds_the_descriptions(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert pipeline["embedded"][0] == [
            "description of alpha", "description of Thing", "description of Thing.go"
        ]

    async def test_upserts_one_vector_per_entity(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        namespace, vectors = pipeline["upserted"][0]
        assert namespace == project.pinecone_namespace
        assert len(vectors) == EXPECTED_ENTITIES

    async def test_vector_ids_are_entity_primary_keys(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        _, vectors = pipeline["upserted"][0]
        vector_ids = {int(v[0]) for v in vectors}

        rows = (
            await db.execute(
                text("SELECT id FROM entities WHERE project_id = :p"), {"p": project.id}
            )
        ).scalars().all()
        assert vector_ids == set(rows)

    async def test_vector_metadata_is_minimal(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        _, vectors = pipeline["upserted"][0]
        for _, _, metadata in vectors:
            assert set(metadata) == {"entity_type"}

    async def test_embeddings_are_the_configured_dimension(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        _, vectors = pipeline["upserted"][0]
        assert all(len(v[1]) == 1024 for v in vectors)

    async def test_temp_directory_is_cleaned_up(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert not os.path.exists(pipeline["dest_dirs"][0])


class TestEmptyRepo:
    async def test_reaches_ready_without_paid_calls(self, db, project, pipeline):
        pipeline["files"].clear()
        pipeline["files"]["README.md"] = "no python here"

        await indexing_service.run_indexing_pipeline(str(project.id))

        status, _ = await status_of(db, project.id)
        assert status == "ready"
        assert pipeline["described"] == []
        assert pipeline["embedded"] == []

    async def test_stores_no_entities(self, db, project, pipeline):
        pipeline["files"].clear()
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert await entity_service.count_for_project(db, project.id) == 0


class TestDedupe:
    async def test_duplicates_are_collapsed_before_the_llm_runs(self, db, project, pipeline):
        """A module-level name bound twice must not reach the LLM twice."""
        pipeline["files"]["m.py"] = (
            "def alpha():\n    return 1\n\n\ndef alpha():\n    return 2\n"
        )
        await indexing_service.run_indexing_pipeline(str(project.id))

        assert pipeline["described"][0].count("alpha") == 1
        assert await entity_service.count_for_project(db, project.id) == 1

    async def test_last_definition_is_the_one_stored(self, db, project, pipeline):
        pipeline["files"]["m.py"] = (
            "def alpha():\n    return 1\n\n\ndef alpha():\n    return 2\n"
        )
        await indexing_service.run_indexing_pipeline(str(project.id))
        stored = (
            await db.execute(
                text("SELECT source_code FROM entities WHERE project_id = :p"), {"p": project.id}
            )
        ).scalar_one()
        assert "return 2" in stored


class TestReindex:
    async def test_identity_is_stable(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        first = set(
            (await db.execute(text("SELECT id FROM entities WHERE project_id = :p"), {"p": project.id})).scalars().all()
        )

        await indexing_service.run_indexing_pipeline(str(project.id))
        second = set(
            (await db.execute(text("SELECT id FROM entities WHERE project_id = :p"), {"p": project.id})).scalars().all()
        )
        assert second == first

    async def test_creates_no_duplicates(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert await entity_service.count_for_project(db, project.id) == EXPECTED_ENTITIES

    async def test_vanished_entities_are_removed_with_their_vectors(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        gone_id = (
            await db.execute(
                text("SELECT id FROM entities WHERE project_id = :p AND qualname = 'alpha'"),
                {"p": project.id},
            )
        ).scalar_one()

        # alpha disappears from the repo on the next run.
        pipeline["files"]["m.py"] = "class Thing:\n    def go(self):\n        pass\n"
        await indexing_service.run_indexing_pipeline(str(project.id))

        remaining = (
            await db.execute(
                text("SELECT qualname FROM entities WHERE project_id = :p"), {"p": project.id}
            )
        ).scalars().all()
        assert "alpha" not in remaining
        assert pipeline["deleted_vectors"][-1] == (project.pinecone_namespace, [str(gone_id)])

    async def test_no_vector_deletion_when_nothing_vanished(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert pipeline["deleted_vectors"] == []


class TestFailures:
    async def test_clone_failure_marks_the_project_failed(self, db, project, pipeline, monkeypatch):
        async def _boom(url, dest):
            raise RuntimeError("git clone failed: repository not found")

        monkeypatch.setattr(github_service, "clone_repo", _boom)
        await indexing_service.run_indexing_pipeline(str(project.id))

        status, error = await status_of(db, project.id)
        assert status == "failed"
        assert "repository not found" in error

    async def test_llm_failure_marks_the_project_failed(self, db, project, pipeline, monkeypatch):
        async def _boom(entities, batch_size=10):
            raise RuntimeError("rate limited")

        monkeypatch.setattr(llm_service, "generate_descriptions_batch", _boom)
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert (await status_of(db, project.id))[0] == "failed"

    async def test_embedding_failure_marks_the_project_failed(self, db, project, pipeline, monkeypatch):
        async def _boom(texts):
            raise RuntimeError("embedding service down")

        monkeypatch.setattr(embedding_service, "embed_texts", _boom)
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert (await status_of(db, project.id))[0] == "failed"

    async def test_pinecone_failure_marks_the_project_failed(self, db, project, pipeline, monkeypatch):
        async def _boom(namespace, vectors):
            raise RuntimeError("pinecone unavailable")

        monkeypatch.setattr(pinecone_service, "upsert_vectors", _boom)
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert (await status_of(db, project.id))[0] == "failed"

    async def test_entities_are_still_persisted_when_pinecone_fails(self, db, project, pipeline, monkeypatch):
        """Postgres is written first, so its rows survive a later failure."""
        async def _boom(namespace, vectors):
            raise RuntimeError("pinecone unavailable")

        monkeypatch.setattr(pinecone_service, "upsert_vectors", _boom)
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert await entity_service.count_for_project(db, project.id) == EXPECTED_ENTITIES

    async def test_temp_directory_is_cleaned_up_after_failure(self, db, project, pipeline, monkeypatch):
        async def _boom(entities, batch_size=10):
            raise RuntimeError("nope")

        monkeypatch.setattr(llm_service, "generate_descriptions_batch", _boom)
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert not os.path.exists(pipeline["dest_dirs"][0])

    async def test_a_later_success_clears_the_error(self, db, project, pipeline, monkeypatch):
        async def _boom(entities, batch_size=10):
            raise RuntimeError("transient")

        monkeypatch.setattr(llm_service, "generate_descriptions_batch", _boom)
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert (await status_of(db, project.id))[1] is not None

        # Restore just this one stub. monkeypatch.undo() would also revert the
        # pipeline fixture's patches, including session_scope, and the retry
        # would not find the (uncommitted) project at all.
        async def _describe(entities, batch_size=10):
            return [f"description of {e.name}" for e in entities]

        monkeypatch.setattr(llm_service, "generate_descriptions_batch", _describe)
        await indexing_service.run_indexing_pipeline(str(project.id))

        status, error = await status_of(db, project.id)
        assert status == "ready"
        assert error is None


class TestMissingProject:
    async def test_unknown_project_is_a_quiet_noop(self, db, pipeline):
        # No exception, and nothing external attempted.
        await indexing_service.run_indexing_pipeline(str(uuid.uuid4()))
        assert pipeline["cloned"] == []
        assert pipeline["statuses"] == []
