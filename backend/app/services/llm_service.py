import asyncio
from openai import AsyncOpenAI
from app.config import settings

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Lazily initialise the OpenAI client so import doesn't fail when the key is empty."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def generate_description(name: str, signature: str, code: str, entity_type: str) -> str:
    """Generate a concise behavioral description of a code entity using an LLM."""
    client = _get_client()
    system_prompt = (
        f"You are a code documentation expert. Given a Python {entity_type}, "
        "write a concise natural-language behavioral description. "
        "Describe WHAT the code does, not HOW. "
        "Focus on purpose, inputs, outputs, and side effects. "
        "Keep it to 2-3 sentences."
    )
    user_prompt = f"Signature:\n{signature}\n\nCode:\n{code}"

    response = await client.chat.completions.create(
        model=settings.openai_llm_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content or ""


async def _process_entity(entity, sem: asyncio.Semaphore) -> str:
    async with sem:
        if isinstance(entity, dict):
            return await generate_description(
                entity.get("name", ""),
                entity.get("signature", ""),
                entity.get("source_code", ""),
                entity.get("entity_type", ""),
            )
        else:
            return await generate_description(
                getattr(entity, "name", ""),
                getattr(entity, "signature", ""),
                getattr(entity, "source_code", ""),
                getattr(entity, "entity_type", ""),
            )


async def generate_descriptions_batch(entities: list, batch_size: int = 10) -> list[str]:
    """Generate descriptions for all entities with bounded concurrency."""
    sem = asyncio.Semaphore(batch_size)
    tasks = [_process_entity(entity, sem) for entity in entities]
    return list(await asyncio.gather(*tasks))
