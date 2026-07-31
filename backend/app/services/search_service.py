from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus
from app.schemas.search import SearchResult
from app.services.embedding_service import embed_query
from app.services.entity_service import get_by_ids, keyword_search
from app.services.pinecone_service import query_vectors

# The constant in reciprocal rank fusion. 60 is the value from the original
# Cormack et al. paper and the usual default. It damps the difference between
# adjacent high ranks: without it, rank 1 would be worth twice rank 2, and a
# single confident-but-wrong retriever would dominate the fusion.
RRF_K = 60

# How deep each retriever goes before fusion. Wider than the result count on
# purpose — the whole point of fusing is to rescue something one retriever
# ranked 8th and the other ranked 12th.
CANDIDATE_DEPTH = 30


def reciprocal_rank_fusion(
    rankings: list[list[int]], *, k: int = RRF_K
) -> list[tuple[int, float]]:
    """Merge ranked id lists into one, by rank rather than by score.

    Fusing on rank is what makes this work across retrievers whose scores are
    not comparable: Pinecone returns a cosine similarity, `ts_rank_cd` returns
    an unbounded relevance figure, and there is no principled way to put those
    on the same axis. Position is the common currency.

    Returns (entity id, fused score), best first. Ties break on the best single
    rank achieved, so a result one retriever put first outranks one that both
    put third.
    """
    scores: dict[int, float] = defaultdict(float)
    best_rank: dict[int, int] = {}
    for ranking in rankings:
        for rank, entity_id in enumerate(ranking, start=1):
            scores[entity_id] += 1.0 / (k + rank)
            best_rank[entity_id] = min(best_rank.get(entity_id, rank), rank)
    ordered = sorted(scores, key=lambda e: (-scores[e], best_rank[e], e))
    return [(entity_id, scores[entity_id]) for entity_id in ordered]


def _normalise(ranked: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Rescale fused scores so the best result is 1.0.

    Raw RRF scores live around 1/60 and mean nothing in isolation — two
    retrievers agreeing on first place scores 0.033. The API exposes `score` as
    a 0–1 relevance the UI renders as a percentage bar, and 3% against a perfect
    match would read as a broken search rather than a good one. Relative to the
    top hit is both displayable and closer to what a ranked bar should convey;
    it is explicitly not a similarity.
    """
    if not ranked:
        return []
    top = ranked[0][1] or 1.0
    return [(entity_id, score / top) for entity_id, score in ranked]


async def dense_candidates(
    project: Project, query: str, depth: int = CANDIDATE_DEPTH
) -> list[tuple[int, float]]:
    """The vector half: (entity id, cosine similarity), best first."""
    embedding = await embed_query(query)
    matches = await query_vectors(
        namespace=project.pinecone_namespace, embedding=embedding, top_k=depth
    )

    # Vector IDs are entity primary keys. Anything unparseable is a leftover
    # from the old positional scheme and is ignored rather than crashing search.
    ranked: list[tuple[int, float]] = []
    for match in matches:
        try:
            entity_id = int(match["id"])
        except (KeyError, TypeError, ValueError):
            continue
        ranked.append((entity_id, match.get("score", 0.0)))
    return ranked


async def hybrid_candidates(
    db: AsyncSession, project: Project, query: str, depth: int = CANDIDATE_DEPTH
) -> list[tuple[int, float]]:
    """Both halves, fused by reciprocal rank. (entity id, score), best first.

    The two retrievers fail in different directions, which is the entire reason
    to run both: the dense half can match a query whose vocabulary appears
    nowhere in the code, and the keyword half cannot be talked out of an exact
    identifier match.

    Measured on the 62-query golden set over this repository: success@5 0.758 ->
    0.790 and MRR 0.592 -> 0.622 against dense alone, gaining two queries and
    losing none. A modest gain, and a strict one.
    """
    dense = [entity_id for entity_id, _ in await dense_candidates(project, query, depth)]
    keyword = await keyword_search(db, project.id, query, limit=depth)
    return _normalise(reciprocal_rank_fusion([dense, keyword]))


async def search_project(
    db: AsyncSession, project: Project, query: str, top_k: int = 10
) -> list[SearchResult]:
    """Rank entities for a query, then hydrate the rows from Postgres.

    Retrieval is hybrid: Pinecone for semantic similarity, Postgres full text
    for exact terms, fused with reciprocal rank. See `hybrid_candidates` for the
    measurement that justifies it.
    """
    if project.status != ProjectStatus.READY:
        raise ValueError(f"Project is not ready for search (status: {project.status.value})")

    ranked = (await hybrid_candidates(db, project, query))[:top_k]

    entities = await get_by_ids(db, project.id, [entity_id for entity_id, _ in ranked])

    results: list[SearchResult] = []
    for entity_id, score in ranked:  # preserve the fused ordering
        entity = entities.get(entity_id)
        if entity is None:
            # A vector whose row is gone: either mid-pipeline or a stale vector.
            # Skipping degrades the result count rather than the request.
            continue
        results.append(
            SearchResult(
                name=entity.qualname,
                entity_type=entity.entity_type,
                code=entity.source_code or "",
                signature=entity.signature or "",
                description=entity.description or "",
                file_path=entity.file_path,
                start_line=entity.start_line or 0,
                end_line=entity.end_line or 0,
                score=score,
            )
        )

    return results
