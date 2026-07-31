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
        # Not Pinecone's 0.83: retrieval is fused, and the exposed score is the
        # RRF score relative to the best hit. A sole result is its own best.
        assert hit.score == 1.0

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

    async def test_a_match_without_a_score_is_still_returned(self, db, ready_project, stub_embedding, stub_pinecone):
        """Pinecone's score no longer reaches the client, but its absence must
        not drop the result: rank is what fusion consumes, not score."""
        ids = await seed(db, ready_project, [ent("m.py", "alpha", "x")], ["d"])
        stub_pinecone["matches"] = [{"id": str(ids[("m.py", "alpha")])}]
        results = await search_service.search_project(db, ready_project, "q")
        assert [r.name for r in results] == ["alpha"]


class TestQueryPlumbing:
    async def test_scopes_the_query_to_the_project_namespace(self, db, ready_project, stub_embedding, stub_pinecone):
        await search_service.search_project(db, ready_project, "q")
        assert stub_pinecone["calls"][0]["namespace"] == ready_project.pinecone_namespace

    async def test_retrieves_deeper_than_it_returns(self, db, ready_project, stub_embedding, stub_pinecone):
        """Fusion needs a wider candidate pool than the result count.

        top_k is applied after fusing, not passed down to Pinecone: the point of
        running two retrievers is to rescue something one ranked 8th and the
        other 12th, which a top_k of 3 at the source would already have thrown
        away.
        """
        entities = [ent("m.py", f"e{i}", "x") for i in range(5)]
        ids = await seed(db, ready_project, entities, ["d"] * 5)
        stub_pinecone["matches"] = [
            {"id": str(ids[("m.py", f"e{i}")]), "score": 1.0 - i / 10} for i in range(5)
        ]

        results = await search_service.search_project(db, ready_project, "q", top_k=3)

        assert stub_pinecone["calls"][0]["top_k"] == search_service.CANDIDATE_DEPTH
        assert len(results) == 3

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


class TestReciprocalRankFusion:
    """Pure ranking arithmetic — no database, no network."""

    def test_empty_input_is_empty(self):
        assert search_service.reciprocal_rank_fusion([]) == []
        assert search_service.reciprocal_rank_fusion([[], []]) == []

    def test_a_single_ranking_is_preserved(self):
        fused = search_service.reciprocal_rank_fusion([[3, 1, 2]])
        assert [e for e, _ in fused] == [3, 1, 2]

    def test_agreement_beats_a_single_first_place(self):
        """The property that makes fusion worth doing.

        7 is first for one retriever and absent from the other; 9 is second for
        both. Two second places outscore one first, which is exactly the
        behaviour that rescues a result neither retriever was sure about.
        """
        fused = search_service.reciprocal_rank_fusion([[7, 9], [8, 9]])
        assert [e for e, _ in fused][0] == 9

    def test_scores_use_the_configured_constant(self):
        fused = dict(search_service.reciprocal_rank_fusion([[5]]))
        assert fused[5] == pytest.approx(1 / (search_service.RRF_K + 1))

    def test_a_result_in_both_lists_sums_its_contributions(self):
        fused = dict(search_service.reciprocal_rank_fusion([[4], [4]]))
        assert fused[4] == pytest.approx(2 / (search_service.RRF_K + 1))

    def test_ties_break_on_the_best_rank_achieved(self):
        """Equal fused scores, but 1 was first somewhere and 2 never was."""
        fused = search_service.reciprocal_rank_fusion([[1, 2], [2, 1]])
        assert [e for e, _ in fused] == [1, 2]

    def test_output_is_sorted_by_descending_score(self):
        fused = search_service.reciprocal_rank_fusion([[1, 2, 3], [3, 2, 1]])
        scores = [s for _, s in fused]
        assert scores == sorted(scores, reverse=True)


class TestHybridRetrieval:
    """The keyword half, and what fusing it in actually changes."""

    @pytest.fixture
    async def corpus(self, db, ready_project):
        """Two entities whose text pulls in different directions."""
        return await seed(
            db,
            ready_project,
            [
                ent("a.py", "backoff_delay", "def backoff_delay(attempts): return 2 ** attempts"),
                ent("b.py", "unrelated", "def unrelated(): pass"),
            ],
            ["how long to wait before retrying", "does something else entirely"],
        )

    async def test_keyword_search_finds_an_exact_identifier(self, db, ready_project, corpus):
        found = await entity_service.keyword_search(db, ready_project.id, "backoff_delay")
        assert found == [corpus[("a.py", "backoff_delay")]]

    async def test_keyword_search_matches_the_description(self, db, ready_project, corpus):
        found = await entity_service.keyword_search(db, ready_project.id, "retrying")
        assert corpus[("a.py", "backoff_delay")] in found

    async def test_keyword_search_accepts_arbitrary_user_text(self, db, ready_project, corpus):
        """websearch_to_tsquery, not to_tsquery: a bare sentence must not raise."""
        found = await entity_service.keyword_search(
            db, ready_project.id, "how long do we wait before retrying? (backoff!)"
        )
        assert corpus[("a.py", "backoff_delay")] in found

    async def test_keyword_search_is_scoped_to_the_project(self, db, user, ready_project, corpus):
        other = make_project(user, status=ProjectStatus.READY)
        db.add(other)
        await db.flush()
        await seed(db, other, [ent("a.py", "backoff_delay", "x")], ["d"])

        found = await entity_service.keyword_search(db, ready_project.id, "backoff_delay")
        assert found == [corpus[("a.py", "backoff_delay")]]

    async def test_blank_query_returns_nothing(self, db, ready_project, corpus):
        assert await entity_service.keyword_search(db, ready_project.id, "   ") == []

    async def test_no_match_returns_nothing(self, db, ready_project, corpus):
        assert await entity_service.keyword_search(db, ready_project.id, "zzzznotaword") == []

    async def test_hybrid_rescues_what_the_dense_half_missed(
        self, db, ready_project, corpus, stub_embedding, stub_pinecone
    ):
        """The measured gain in miniature: Pinecone ranks the wrong entity
        first, and the keyword half pulls the right one back to the top."""
        stub_pinecone["matches"] = [{"id": str(corpus[("b.py", "unrelated")]), "score": 0.9}]

        ranked = await search_service.hybrid_candidates(db, ready_project, "backoff_delay")
        assert [e for e, _ in ranked][0] == corpus[("a.py", "backoff_delay")]

    async def test_scores_are_normalised_to_the_best_hit(
        self, db, ready_project, corpus, stub_embedding, stub_pinecone
    ):
        """Raw RRF sits near 1/60; the UI renders score as a percentage bar."""
        stub_pinecone["matches"] = [{"id": str(corpus[("b.py", "unrelated")]), "score": 0.9}]

        ranked = await search_service.hybrid_candidates(db, ready_project, "backoff_delay")
        assert ranked[0][1] == 1.0
        assert all(0.0 < score <= 1.0 for _, score in ranked)

    async def test_dense_only_still_works_when_keyword_finds_nothing(
        self, db, ready_project, corpus, stub_embedding, stub_pinecone
    ):
        stub_pinecone["matches"] = [{"id": str(corpus[("a.py", "backoff_delay")]), "score": 0.9}]

        ranked = await search_service.hybrid_candidates(db, ready_project, "zzzznotaword")
        assert [e for e, _ in ranked] == [corpus[("a.py", "backoff_delay")]]
