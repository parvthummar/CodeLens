from openai import AsyncOpenAI
from app.config import settings

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Lazily initialise the OpenAI client so import doesn't fail when the key is empty."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, batching at 100 per API call."""
    client = _get_client()
    all_embeddings: list[list[float]] = []
    batch_size = 100
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        response = await client.embeddings.create(
            input=batch,
            model=settings.openai_embedding_model,
            dimensions=settings.openai_embedding_dimensions,
        )
        batch_embeddings = [data.embedding for data in response.data]
        all_embeddings.extend(batch_embeddings)
    return all_embeddings


async def embed_query(text: str) -> list[float]:
    """Embed a single query text and return its vector."""
    client = _get_client()
    response = await client.embeddings.create(
        input=[text],
        model=settings.openai_embedding_model,
        dimensions=settings.openai_embedding_dimensions,
    )
    return response.data[0].embedding
