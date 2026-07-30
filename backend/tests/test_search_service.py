"""Search: Pinecone supplies ranking, Postgres supplies content."""

import pytest

from app.models.project import ProjectStatus
from app.services import entity_service, search_service
from app.services.parser_service import CodeEntity
from tests.conftest import make_project

pytestmark = pytest.mark.db


def ent(file_path: str, name: str, code: str, kind: str = "function") -> CodeEntity:
    return CodeEntity(
        name=name,
        entity_type=kind,
        source_code=code,
        signature=f"def {name}():",
        file_path=file_path,
        start_line=10,
        end_line=20,
    )


@pytest.fixture
def stub_embedding(monkeypatch):
    """search_service imports embed_query directly, so patch it there."""

    async def _embed_query(text: str) -> list[float]:
        return [0.1] * 1024

    monkeypatch.setattr(search_service, "embed_query", _embed_query)


@pytest.fixture
def stub_pinecone(monkeypatch):
    """Returns whatever matches the test sets, and records the query arguments."""
    state = {"matches": [], "calls": []}

    async def _query_vectors(namespace, embedding, top_k=10):
        state["calls"].append({"namespace": namespace, "top_k": top_k, "dims": len(embedding)})
        return state["matches"]

    monkeypatch.setattr(search_service, "query_vectors", _query_vectors)
    return state


@pytest.fixture
async def ready_project(db, user):
    record = make_project(user, status=ProjectStatus.READY)
    db.add(record)
    await db.flush()
    return record


async def seed(db, project, entities, descriptions):
    return await entity_service.upsert_entities(
        db, entity_service.build_rows(project.id, entities, descriptions)
    )


class TestStatusGuard:
    @pytest.mark.parametrize(
        "status",
        [ProjectStatus.PENDING, ProjectStatus.CLONING, ProjectStatus.INDEXING, ProjectStatus.FAILED],
    )
    async def test_rejects_projects_that_are_not_ready(self, db, user, status):
        record = make_project(user, status=status)
        db.add(record)
        await db.flush()
        with pytest.raises(ValueError, match="not ready"):
            await search_service.search_project(db, record, "anything")

    async def test_guard_runs_before_any_paid_call(self, db, user):
        """No stubs installed here - the autouse guard fails on a real client."""
        record = make_project(user, status=ProjectStatus.PENDING)
        db.add(record)
        await db.flush()
        with pytest.raises(ValueError):
            await search_service.search_project(db, record, "anything")


class TestHydration:
    async def test_returns_content_from_postgres(self, db, ready_project, stub_embedding, stub_pinecone):
        ids = await seed(db, ready_project, [ent("pkg/m.py", "alpha", "def alpha():\n    return 1")], ["describes alpha"])
        stub_pinecone["matches"] = [{"id": str(ids[("pkg/m.py", "alpha")]), "score": 0.83}]

        results = await search_service.search_project(db, ready_project, "q")

        assert len(results) == 1
        hit = results[0]
        assert hit.name == "alpha"
        assert hit.code == "def alpha():\n    return 1"
        assert hit.description == "describes alpha"
        assert hit.file_path == "pkg/m.py"
        assert hit.signature == "def alpha():"
        assert hit.start_line == 10 and hit.end_line == 20
        assert hit.score == 0.83

    async def test_method_qualname_becomes_the_name(self, db, ready_project, stub_embedding, stub_pinecone):
        ids = await seed(db, ready_project, [ent("m.py", "Thing.go", "x", "method")], ["d"])
        stub_pinecone["matches"] = [{"id": str(ids[("m.py", "Thing.go")]), "score": 0.5}]
        results = await search_service.search_project(db, ready_project, "q")
        assert results[0].name == "Thing.go"
        assert results[0].entity_type == "method"

    async def test_preserves_pinecone_ranking(self, db, ready_project, stub_embedding, stub_pinecone):
        """Ordering comes from Pinecone, not from the SQL hydration query."""
        ids = await seed(
            db, ready_project,
            [ent("m.py", "first", "a"), ent("m.py", "second", "b"), ent("m.py", "third", "c")],
            ["d", "d", "d"],
        )
        stub_pinecone["matches"] = [
            {"id": str(ids[("m.py", "third")]), "score": 0.9},
            {"id": str(ids[("m.py", "first")]), "score": 0.7},
            {"id": str(ids[("m.py", "second")]), "score": 0.5},
        ]
        results = await search_service.search_project(db, ready_project, "q")
        assert [r.name for r in results] == ["third", "first", "second"]

    async def test_null_text_columns_become_empty_strings(self, db, ready_project, stub_embedding, stub_pinecone):
        """SearchResult requires str; the entity columns are nullable."""
        from sqlalchemy import text

        ids = await seed(db, ready_project, [ent("m.py", "alpha", "x")], ["d"])
        entity_id = ids[("m.py", "alpha")]
        await db.execute(
            text("UPDATE entities SET source_code=NULL, signature=NULL, description=NULL WHERE id=:i"),
            {"i": entity_id},
        )
        db.expunge_all()
        stub_pinecone["matches"] = [{"id": str(entity_id), "score": 0.5}]

        hit = (await search_service.search_project(db, ready_project, "q"))[0]
        assert hit.code == "" and hit.signature == "" and hit.description == ""


class TestResilience:
    async def test_no_matches_gives_no_results(self, db, ready_project, stub_embedding, stub_pinecone):
        stub_pinecone["matches"] = []
        assert await search_service.search_project(db, ready_project, "q") == []

    async def test_skips_vectors_whose_row_is_gone(self, db, ready_project, stub_embedding, stub_pinecone):
        """Can happen if a run died between the Postgres and Pinecone writes."""
        ids = await seed(db, ready_project, [ent("m.py", "alpha", "x")], ["d"])
        stub_pinecone["matches"] = [
            {"id": str(ids[("m.py", "alpha")]), "score": 0.9},
            {"id": "999999999999", "score": 0.8},
        ]
        results = await search_service.search_project(db, ready_project, "q")
        assert [r.name for r in results] == ["alpha"]

    async def test_ignores_legacy_positional_ids(self, db, ready_project, stub_embedding, stub_pinecone):
        """Old vectors were named "{project_id}_{i}" and are not integers."""
        ids = await seed(db, ready_project, [ent("m.py", "alpha", "x")], ["d"])
        stub_pinecone["matches"] = [
            {"id": f"{ready_project.id}_3", "score": 0.95},
            {"id": str(ids[("m.py", "alpha")]), "score": 0.6},
        ]
        results = await search_service.search_project(db, ready_project, "q")
        assert [r.name for r in results] == ["alpha"]

    async def test_tolerates_matches_without_an_id(self, db, ready_project, stub_embedding, stub_pinecone):
        stub_pinecone["matches"] = [{"score": 0.5}]
        assert await search_service.search_project(db, ready_project, "q") == []

    async def test_missing_score_defaults_to_zero(self, db, ready_project, stub_embedding, stub_pinecone):
        ids = await seed(db, ready_project, [ent("m.py", "alpha", "x")], ["d"])
        stub_pinecone["matches"] = [{"id": str(ids[("m.py", "alpha")])}]
        assert (await search_service.search_project(db, ready_project, "q"))[0].score == 0.0


class TestQueryPlumbing:
    async def test_scopes_the_query_to_the_project_namespace(self, db, ready_project, stub_embedding, stub_pinecone):
        await search_service.search_project(db, ready_project, "q")
        assert stub_pinecone["calls"][0]["namespace"] == ready_project.pinecone_namespace

    async def test_passes_top_k_through(self, db, ready_project, stub_embedding, stub_pinecone):
        await search_service.search_project(db, ready_project, "q", top_k=3)
        assert stub_pinecone["calls"][0]["top_k"] == 3

    async def test_embeds_at_the_configured_dimension(self, db, ready_project, stub_embedding, stub_pinecone):
        await search_service.search_project(db, ready_project, "q")
        assert stub_pinecone["calls"][0]["dims"] == 1024

    async def test_does_not_return_another_projects_entities(self, db, user, ready_project, stub_embedding, stub_pinecone):
        """A stale or mismatched vector id must not leak another project's code."""
        other = make_project(user, status=ProjectStatus.READY)
        db.add(other)
        await db.flush()
        other_ids = await seed(db, other, [ent("m.py", "secret", "x")], ["d"])

        # Pinecone (wrongly) hands back an id belonging to the other project.
        stub_pinecone["matches"] = [{"id": str(other_ids[("m.py", "secret")]), "score": 0.9}]
        assert await search_service.search_project(db, ready_project, "q") == []
