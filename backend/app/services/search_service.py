from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus
from app.schemas.search import SearchResult
from app.services.embedding_service import embed_query
from app.services.entity_service import get_by_ids
from app.services.pinecone_service import query_vectors


async def search_project(
    db: AsyncSession, project: Project, query: str, top_k: int = 10
) -> list[SearchResult]:
    """Embed the query, rank ids in Pinecone, then hydrate rows from Postgres."""
    if project.status != ProjectStatus.READY:
        raise ValueError(f"Project is not ready for search (status: {project.status.value})")

    query_embedding = await embed_query(query)

    matches = await query_vectors(
        namespace=project.pinecone_namespace,
        embedding=query_embedding,
        top_k=top_k,
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

    entities = await get_by_ids(db, project.id, [entity_id for entity_id, _ in ranked])

    results: list[SearchResult] = []
    for entity_id, score in ranked:  # preserve Pinecone's ordering
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
