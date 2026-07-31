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

from app.models.project import ProjectStatus
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
        # Mutable so a test can simulate the repo moving to a new commit.
        "head": ["a" * 40],
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

    async def _head_commit(dest):
        return calls["head"][-1]

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

    original_set_status = indexing_service.set_project_status

    async def _spy_set_status(project_id, status, *, error_message=None):
        calls["statuses"].append(status.value)
        await original_set_status(project_id, status, error_message=error_message)

    monkeypatch.setattr(indexing_service, "session_scope", _scope)
    monkeypatch.setattr(github_service, "clone_repo", _clone)
    monkeypatch.setattr(github_service, "head_commit", _head_commit)
    monkeypatch.setattr(llm_service, "generate_descriptions_batch", _describe)
    monkeypatch.setattr(embedding_service, "embed_texts", _embed)
    monkeypatch.setattr(pinecone_service, "upsert_vectors", _upsert)
    monkeypatch.setattr(pinecone_service, "delete_vectors", _delete_vectors)
    monkeypatch.setattr(indexing_service, "set_project_status", _spy_set_status)

    calls["files"] = files
    return calls


async def status_of(db, project_id) -> tuple[str, str | None]:
    row = (
        await db.execute(
            text("SELECT status, error_message FROM projects WHERE id = :i"), {"i": project_id}
        )
    ).one()
    return row[0], row[1]


async def commit_of(db, project_id) -> str | None:
    return (
        await db.execute(
            text("SELECT last_indexed_commit FROM projects WHERE id = :i"),
            {"i": project_id},
        )
    ).scalar_one()


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


class TestIncrementalReindex:
    """Only genuinely changed entities reach the LLM and the embedding model.

    `content_hash` has been stored since the migration; this is what consumes
    it. The assertions are call counts rather than timings because the cost
    being saved is per-call, and a stub makes it exactly countable.
    """

    async def described_names(self, pipeline) -> list[str]:
        return [name for batch in pipeline["described"] for name in batch]

    async def test_an_unchanged_reindex_makes_no_paid_calls(self, db, project, pipeline):
        """The headline: re-indexing a repo that has not moved costs nothing."""
        await indexing_service.run_indexing_pipeline(str(project.id))
        pipeline["described"].clear()
        pipeline["embedded"].clear()
        pipeline["upserted"].clear()

        await indexing_service.run_indexing_pipeline(str(project.id))

        assert pipeline["described"] == []
        assert pipeline["embedded"] == []
        assert pipeline["upserted"] == []

    async def test_an_unchanged_reindex_still_reaches_ready(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        await indexing_service.run_indexing_pipeline(str(project.id))

        status, error = await status_of(db, project.id)
        assert status == "ready"
        assert error is None
        assert await entity_service.count_for_project(db, project.id) == EXPECTED_ENTITIES

    async def test_one_edited_function_costs_exactly_one_of_everything(
        self, db, project, pipeline
    ):
        """The roadmap's verification criterion, as an assertion."""
        await indexing_service.run_indexing_pipeline(str(project.id))
        pipeline["described"].clear()
        pipeline["embedded"].clear()
        pipeline["upserted"].clear()

        # alpha's body changes; Thing and Thing.go are untouched.
        pipeline["files"]["m.py"] = SOURCE.replace("return 1", "return 999")
        await indexing_service.run_indexing_pipeline(str(project.id))

        assert await self.described_names(pipeline) == ["alpha"]
        assert pipeline["embedded"] == [["description of alpha"]]
        assert [len(vectors) for _, vectors in pipeline["upserted"]] == [1]

    async def test_the_edit_lands_in_postgres(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        pipeline["files"]["m.py"] = SOURCE.replace("return 1", "return 999")
        await indexing_service.run_indexing_pipeline(str(project.id))

        source = (
            await db.execute(
                text("SELECT source_code FROM entities WHERE project_id = :p AND qualname = 'alpha'"),
                {"p": project.id},
            )
        ).scalar_one()
        assert "return 999" in source

    async def test_skipped_entities_keep_their_identity_and_description(
        self, db, project, pipeline
    ):
        """Reuse must not mean "left behind" — the rows have to stay intact."""
        await indexing_service.run_indexing_pipeline(str(project.id))
        before = dict(
            (
                await db.execute(
                    text("SELECT qualname, id FROM entities WHERE project_id = :p"),
                    {"p": project.id},
                )
            ).all()
        )

        pipeline["files"]["m.py"] = SOURCE.replace("return 1", "return 999")
        await indexing_service.run_indexing_pipeline(str(project.id))

        after = dict(
            (
                await db.execute(
                    text("SELECT qualname, description FROM entities WHERE project_id = :p"),
                    {"p": project.id},
                )
            ).all()
        )
        ids_after = dict(
            (
                await db.execute(
                    text("SELECT qualname, id FROM entities WHERE project_id = :p"),
                    {"p": project.id},
                )
            ).all()
        )
        assert ids_after == before
        assert after["Thing.go"] == "description of Thing.go"

    async def test_a_new_function_is_the_only_one_described(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        pipeline["described"].clear()

        pipeline["files"]["m.py"] = SOURCE + "\n\ndef beta():\n    return 2\n"
        await indexing_service.run_indexing_pipeline(str(project.id))

        assert await self.described_names(pipeline) == ["beta"]
        assert await entity_service.count_for_project(db, project.id) == EXPECTED_ENTITIES + 1

    async def test_a_shifted_function_is_relocated_not_redescribed(
        self, db, project, pipeline
    ):
        """Identical source at new line numbers: fix the location, pay nothing.

        content_hash covers the source alone, so this bucket exists precisely
        because hashing the location too would have turned every insertion at
        the top of a file into a full re-index of that file.
        """
        await indexing_service.run_indexing_pipeline(str(project.id))
        pipeline["described"].clear()
        pipeline["embedded"].clear()

        # Three comment lines above everything: same code, all of it moved.
        pipeline["files"]["m.py"] = "# a\n# b\n# c\n" + SOURCE
        await indexing_service.run_indexing_pipeline(str(project.id))

        assert pipeline["described"] == []
        assert pipeline["embedded"] == []

        start = (
            await db.execute(
                text("SELECT start_line FROM entities WHERE project_id = :p AND qualname = 'alpha'"),
                {"p": project.id},
            )
        ).scalar_one()
        assert start == 4

    async def test_a_rename_describes_the_new_name_and_drops_the_old(
        self, db, project, pipeline
    ):
        await indexing_service.run_indexing_pipeline(str(project.id))
        pipeline["described"].clear()

        pipeline["files"]["m.py"] = SOURCE.replace("def alpha", "def renamed")
        await indexing_service.run_indexing_pipeline(str(project.id))

        assert await self.described_names(pipeline) == ["renamed"]
        names = (
            await db.execute(
                text("SELECT qualname FROM entities WHERE project_id = :p"), {"p": project.id}
            )
        ).scalars().all()
        assert "alpha" not in names
        assert "renamed" in names

    async def test_reports_the_saving(self, db, project, pipeline):
        first = await indexing_service.run_indexing_pipeline(str(project.id))
        assert first.entities_described == EXPECTED_ENTITIES
        assert first.entities_reused == 0
        assert first.reuse_ratio == 0.0

        pipeline["files"]["m.py"] = SOURCE.replace("return 1", "return 999")
        second = await indexing_service.run_indexing_pipeline(str(project.id))
        assert second.entities_described == 1
        assert second.entities_reused == 2
        assert second.entities_indexed == EXPECTED_ENTITIES
        assert round(second.reuse_ratio, 3) == round(2 / 3, 3)

    async def test_progress_starts_from_what_was_reused(self, db, project, pipeline):
        """A re-index that skips most of the repo should not look like a restart."""
        await indexing_service.run_indexing_pipeline(str(project.id))

        seen = []

        async def _on_progress(done, total):
            seen.append((done, total))

        pipeline["files"]["m.py"] = SOURCE.replace("return 1", "return 999")
        await indexing_service.run_indexing_pipeline(
            str(project.id), on_progress=_on_progress
        )
        assert seen == [(2, 3), (3, 3)]

    async def test_a_retry_does_not_redescribe_the_chunk_that_landed(
        self, db, project, pipeline, monkeypatch
    ):
        """Step 3 left this open: per-chunk commits were durable but not reused.

        The first attempt writes chunk one and dies on chunk two. The retry has
        to describe only what is still missing.
        """
        monkeypatch.setattr(indexing_service.settings, "indexing_chunk_size", 2)

        real_embed = embedding_service.embed_texts
        state = {"calls": 0}

        async def _fail_on_second(texts):
            state["calls"] += 1
            if state["calls"] == 2:
                raise RuntimeError("embedding service down")
            return await real_embed(texts)

        monkeypatch.setattr(embedding_service, "embed_texts", _fail_on_second)
        with pytest.raises(RuntimeError):
            await indexing_service.run_indexing_pipeline(str(project.id))

        assert await entity_service.count_for_project(db, project.id) == 2
        monkeypatch.setattr(embedding_service, "embed_texts", real_embed)
        pipeline["described"].clear()

        await indexing_service.run_indexing_pipeline(str(project.id))

        # One entity left, not all three.
        assert len(await self.described_names(pipeline)) == 1
        assert await entity_service.count_for_project(db, project.id) == EXPECTED_ENTITIES
        assert (await status_of(db, project.id))[0] == "ready"


class TestCommitSha:
    async def test_stored_after_a_successful_run(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert await commit_of(db, project.id) == "a" * 40

    async def test_reported_in_the_result(self, db, project, pipeline):
        result = await indexing_service.run_indexing_pipeline(str(project.id))
        assert result.commit_sha == "a" * 40

    async def test_updated_when_the_repo_moves_on(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        pipeline["head"].append("b" * 40)
        pipeline["files"]["m.py"] = SOURCE.replace("return 1", "return 999")

        await indexing_service.run_indexing_pipeline(str(project.id))
        assert await commit_of(db, project.id) == "b" * 40

    async def test_not_advanced_by_a_failed_run(self, db, project, pipeline, monkeypatch):
        """The column names a commit that is fully present in both stores."""
        await indexing_service.run_indexing_pipeline(str(project.id))

        async def _boom(namespace, vectors):
            raise RuntimeError("pinecone unavailable")

        pipeline["head"].append("b" * 40)
        pipeline["files"]["m.py"] = SOURCE.replace("return 1", "return 999")
        monkeypatch.setattr(pinecone_service, "upsert_vectors", _boom)

        with pytest.raises(RuntimeError):
            await indexing_service.run_indexing_pipeline(str(project.id))
        assert await commit_of(db, project.id) == "a" * 40

    async def test_an_unresolvable_head_is_not_fatal(self, db, project, pipeline, monkeypatch):
        async def _no_head(dest):
            return None

        monkeypatch.setattr(github_service, "head_commit", _no_head)
        await indexing_service.run_indexing_pipeline(str(project.id))

        assert (await status_of(db, project.id))[0] == "ready"
        assert await commit_of(db, project.id) is None


class TestFailures:
    """The pipeline raises; it does not decide what a failure means.

    Classifying a failure — transient and worth retrying, or terminal — belongs
    to the worker and job_service. Swallowing exceptions here is what previously
    made a rate limit indistinguishable from a repo that will never parse.
    """

    async def test_clone_failure_propagates(self, db, project, pipeline, monkeypatch):
        async def _boom(url, dest):
            raise RuntimeError("git clone failed: repository not found")

        monkeypatch.setattr(github_service, "clone_repo", _boom)

        with pytest.raises(RuntimeError, match="repository not found"):
            await indexing_service.run_indexing_pipeline(str(project.id))

    async def test_llm_failure_propagates(self, db, project, pipeline, monkeypatch):
        async def _boom(entities, batch_size=10):
            raise RuntimeError("rate limited")

        monkeypatch.setattr(llm_service, "generate_descriptions_batch", _boom)

        with pytest.raises(RuntimeError, match="rate limited"):
            await indexing_service.run_indexing_pipeline(str(project.id))

    async def test_embedding_failure_propagates(self, db, project, pipeline, monkeypatch):
        async def _boom(texts):
            raise RuntimeError("embedding service down")

        monkeypatch.setattr(embedding_service, "embed_texts", _boom)

        with pytest.raises(RuntimeError, match="embedding service down"):
            await indexing_service.run_indexing_pipeline(str(project.id))

    async def test_pinecone_failure_propagates(self, db, project, pipeline, monkeypatch):
        async def _boom(namespace, vectors):
            raise RuntimeError("pinecone unavailable")

        monkeypatch.setattr(pinecone_service, "upsert_vectors", _boom)

        with pytest.raises(RuntimeError, match="pinecone unavailable"):
            await indexing_service.run_indexing_pipeline(str(project.id))

    async def test_the_project_is_not_marked_failed_here(self, db, project, pipeline, monkeypatch):
        """A retryable failure must not surface to the user as 'failed'."""
        async def _boom(entities, batch_size=10):
            raise RuntimeError("rate limited")

        monkeypatch.setattr(llm_service, "generate_descriptions_batch", _boom)
        with pytest.raises(RuntimeError):
            await indexing_service.run_indexing_pipeline(str(project.id))

        assert (await status_of(db, project.id))[0] != "failed"

    async def test_entities_are_still_persisted_when_pinecone_fails(self, db, project, pipeline, monkeypatch):
        """Postgres is written first, so its rows survive a later failure."""
        async def _boom(namespace, vectors):
            raise RuntimeError("pinecone unavailable")

        monkeypatch.setattr(pinecone_service, "upsert_vectors", _boom)
        with pytest.raises(RuntimeError):
            await indexing_service.run_indexing_pipeline(str(project.id))

        assert await entity_service.count_for_project(db, project.id) == EXPECTED_ENTITIES

    async def test_temp_directory_is_cleaned_up_after_failure(self, db, project, pipeline, monkeypatch):
        async def _boom(entities, batch_size=10):
            raise RuntimeError("nope")

        monkeypatch.setattr(llm_service, "generate_descriptions_batch", _boom)
        with pytest.raises(RuntimeError):
            await indexing_service.run_indexing_pipeline(str(project.id))

        assert not os.path.exists(pipeline["dest_dirs"][0])

    async def test_a_later_success_clears_the_error(self, db, project, pipeline, monkeypatch):
        await indexing_service.set_project_status(
            project.id, ProjectStatus.FAILED, error_message="an earlier attempt"
        )
        assert (await status_of(db, project.id))[1] is not None

        await indexing_service.run_indexing_pipeline(str(project.id))

        status, error = await status_of(db, project.id)
        assert status == "ready"
        assert error is None


class TestChunking:
    """Entities flow through in chunks instead of being staged whole.

    This is what bounds peak memory by chunk size rather than repo size, and
    what makes each chunk durable before the next one starts.
    """

    @pytest.fixture
    def small_chunks(self, monkeypatch):
        monkeypatch.setattr(indexing_service.settings, "indexing_chunk_size", 2)

    async def test_describes_and_embeds_one_chunk_at_a_time(
        self, db, project, pipeline, small_chunks
    ):
        # 3 entities at a chunk size of 2 -> two passes, not one.
        await indexing_service.run_indexing_pipeline(str(project.id))

        assert [len(c) for c in pipeline["described"]] == [2, 1]
        assert [len(e) for e in pipeline["embedded"]] == [2, 1]

    async def test_upserts_vectors_per_chunk(self, db, project, pipeline, small_chunks):
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert [len(v) for _, v in pipeline["upserted"]] == [2, 1]

    async def test_every_entity_still_lands_exactly_once(
        self, db, project, pipeline, small_chunks
    ):
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert await entity_service.count_for_project(db, project.id) == EXPECTED_ENTITIES

    async def test_reports_progress_after_each_chunk(
        self, db, project, pipeline, small_chunks
    ):
        seen = []

        async def _on_progress(done, total):
            seen.append((done, total))

        await indexing_service.run_indexing_pipeline(
            str(project.id), on_progress=_on_progress
        )
        # An initial zero so total is known up front, then one per chunk.
        assert seen == [(0, 3), (2, 3), (3, 3)]

    async def test_progress_never_overstates_what_landed(
        self, db, project, pipeline, small_chunks, monkeypatch
    ):
        """Checkpoint after both stores have the chunk, not before."""
        seen = []

        async def _on_progress(done, total):
            stored = await entity_service.count_for_project(db, project.id)
            seen.append((done, stored))

        await indexing_service.run_indexing_pipeline(
            str(project.id), on_progress=_on_progress
        )
        assert all(done <= stored for done, stored in seen)

    async def test_an_earlier_chunk_survives_a_later_failure(
        self, db, project, pipeline, small_chunks, monkeypatch
    ):
        """The whole point of chunking: work already done is not thrown away."""
        real_embed = embedding_service.embed_texts
        state = {"calls": 0}

        async def _fail_on_second(texts):
            state["calls"] += 1
            if state["calls"] == 2:
                raise RuntimeError("embedding service down")
            return await real_embed(texts)

        monkeypatch.setattr(embedding_service, "embed_texts", _fail_on_second)

        with pytest.raises(RuntimeError):
            await indexing_service.run_indexing_pipeline(str(project.id))

        # The first chunk is committed; the staged pipeline would have kept
        # everything in memory and lost all of it.
        assert await entity_service.count_for_project(db, project.id) == 2

    async def test_deletion_waits_for_the_whole_run(
        self, db, project, pipeline, small_chunks
    ):
        """delete_missing is relative to the complete run. Per chunk, it would
        delete everything the later chunks were about to write."""
        await indexing_service.run_indexing_pipeline(str(project.id))
        assert pipeline["deleted_vectors"] == []
        assert await entity_service.count_for_project(db, project.id) == EXPECTED_ENTITIES


class TestResult:
    async def test_reports_what_it_did(self, db, project, pipeline):
        result = await indexing_service.run_indexing_pipeline(str(project.id))
        assert result.entities_indexed == EXPECTED_ENTITIES
        assert result.entities_removed == 0
        assert result.chunks == 1

    async def test_counts_removals(self, db, project, pipeline):
        await indexing_service.run_indexing_pipeline(str(project.id))
        pipeline["files"]["m.py"] = "class Thing:\n    def go(self):\n        pass\n"

        result = await indexing_service.run_indexing_pipeline(str(project.id))
        assert result.entities_removed == 1


class TestMissingProject:
    async def test_unknown_project_is_a_quiet_noop(self, db, pipeline):
        # Not an error: a project deleted mid-flight is a normal outcome.
        assert await indexing_service.run_indexing_pipeline(str(uuid.uuid4())) is None
        assert pipeline["cloned"] == []
        assert pipeline["statuses"] == []
