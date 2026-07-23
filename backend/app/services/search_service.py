from app.models.project import Project, ProjectStatus
from app.services.embedding_service import embed_query
from app.services.pinecone_service import query_vectors
from app.schemas.search import SearchResult


async def search_project(project: Project, query: str, top_k: int = 10) -> list[SearchResult]:
    """Embed the query and search against the project's Pinecone namespace."""
    if project.status != ProjectStatus.READY:
        raise ValueError(f"Project is not ready for search (status: {project.status.value})")

    # 1. Embed the query
    query_embedding = await embed_query(query)

    # 2. Query Pinecone
    matches = await query_vectors(
        namespace=project.pinecone_namespace,
        embedding=query_embedding,
        top_k=top_k,
    )

    # 3. Convert matches to SearchResult objects
    results = []
    for match in matches:
        metadata = match.get("metadata", {})
        results.append(
            SearchResult(
                name=metadata.get("name", ""),
                entity_type=metadata.get("entity_type", ""),
                code=metadata.get("code", ""),
                signature=metadata.get("signature", ""),
                description=metadata.get("description", ""),
                file_path=metadata.get("file_path", ""),
                start_line=metadata.get("start_line", 0),
                end_line=metadata.get("end_line", 0),
                score=match.get("score", 0.0),
            )
        )

    return results
