from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie

from app.config import settings
from app.models.user import User
from app.models.project import Project

_client: AsyncIOMotorClient | None = None


async def init_db() -> None:
    """Initialise Motor client and Beanie ODM."""
    global _client
    _client = AsyncIOMotorClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=5000,
    )
    await init_beanie(
        database=_client[settings.mongo_db_name],
        document_models=[User, Project],
    )


async def close_db() -> None:
    """Close the Motor client connection."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
