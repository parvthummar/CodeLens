import asyncio
from pinecone import Pinecone
from app.config import settings

_index = None


def _get_index():
    """Lazily initialise the Pinecone index.

    Supports two modes:
    - If pinecone_index_host is set, connects directly via host URL (faster, no list-indexes call).
    - Otherwise falls back to looking up the index by name.
    """
    global _index
    if _index is None:
        pc = Pinecone(api_key=settings.pinecone_api_key)
        if settings.pinecone_index_host:
            _index = pc.Index(
                name=settings.pinecone_index_name,
                host=settings.pinecone_index_host,
            )
        else:
            _index = pc.Index(settings.pinecone_index_name)
    return _index


async def upsert_vectors(namespace: str, vectors: list[tuple[str, list[float], dict]]) -> None:
    """Upsert vectors into a Pinecone namespace in batches of 100."""
    index = _get_index()
    batch_size = 100

    def _upsert_chunk(chunk):
        index.upsert(vectors=chunk, namespace=namespace)

    for i in range(0, len(vectors), batch_size):
        chunk = vectors[i : i + batch_size]
        await asyncio.to_thread(_upsert_chunk, chunk)


async def query_vectors(namespace: str, embedding: list[float], top_k: int = 10) -> list[dict]:
    """Query the Pinecone namespace and return matches with metadata."""
    index = _get_index()

    def _query():
        return index.query(
            vector=embedding, top_k=top_k, namespace=namespace, include_metadata=True
        )

    result = await asyncio.to_thread(_query)
    return result.to_dict().get("matches", [])


async def delete_namespace(namespace: str) -> None:
    """Delete all vectors in a Pinecone namespace."""
    index = _get_index()
    await asyncio.to_thread(index.delete, delete_all=True, namespace=namespace)
