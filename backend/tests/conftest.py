"""Shared test fixtures.

Two things worth understanding before adding tests here:

1. Database isolation uses SAVEPOINTs, not "don't commit". Services legitimately
   call `db.commit()`, and we do not want test-only branches in production code,
   so the session is bound to a connection whose outer transaction is always
   rolled back. `join_transaction_mode="create_savepoint"` turns each
   `commit()` into a savepoint release.

2. HTTP tests use httpx's ASGITransport rather than TestClient. TestClient runs
   the app in its own thread and event loop, and asyncpg connections are bound
   to the loop that created them — sharing the test session across that boundary
   does not work.
"""

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.postgres import build_engine_from_url, get_db
from app.models.project import Project, ProjectStatus
from app.models.user import User


# --------------------------------------------------------------------------- #
# Safety net: no test may reach a paid external API.
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _block_external_calls(monkeypatch):
    """Make the OpenAI and Pinecone clients unusable.

    Tests stub the service-level functions instead. If a stub is missing, the
    test fails loudly here rather than quietly spending money.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError(
            "test attempted a real external API call - stub the service function"
        )

    from app.services import embedding_service, llm_service, pinecone_service

    monkeypatch.setattr(llm_service, "_get_client", _boom)
    monkeypatch.setattr(embedding_service, "_get_client", _boom)
    monkeypatch.setattr(pinecone_service, "_get_index", _boom)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture(scope="session")
async def engine():
    """One engine for the whole run, against the direct (non-pooler) endpoint."""
    url = settings.database_url_direct or settings.database_url
    if not url:
        pytest.skip("neither DATABASE_URL_DIRECT nor DATABASE_URL is set")
    eng = build_engine_from_url(url)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    """A session whose work is always rolled back. See the module docstring."""
    async with engine.connect() as conn:
        outer = await conn.begin()
        sessions = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        session = sessions()
        try:
            yield session
        finally:
            await session.close()
            await outer.rollback()


@pytest_asyncio.fixture
async def client(db):
    """HTTP client with the request session replaced by the test session.

    The lifespan is deliberately not run: it would build a second engine, and
    every route resolves its session through this override anyway.
    """
    from app.main import app

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def user(db) -> User:
    from app.core.security import hash_password

    record = User(
        email=f"test-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password=hash_password("correct-horse"),
        full_name="Test User",
    )
    db.add(record)
    await db.flush()
    return record


@pytest_asyncio.fixture
async def other_user(db) -> User:
    from app.core.security import hash_password

    record = User(
        email=f"other-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password=hash_password("correct-horse"),
    )
    db.add(record)
    await db.flush()
    return record


def make_project(owner: User, **overrides) -> Project:
    """Build a Project for `owner`; pass overrides for anything that matters."""
    project_id = overrides.pop("id", uuid.uuid4())
    fields = {
        "id": project_id,
        "user_id": owner.id,
        "name": "demo",
        "github_repo_url": "https://github.com/octocat/Hello-World",
        "github_owner": "octocat",
        "github_repo_name": "Hello-World",
        "pinecone_namespace": str(project_id),
        "status": ProjectStatus.PENDING,
    }
    fields.update(overrides)
    return Project(**fields)


@pytest_asyncio.fixture
async def project(db, user) -> Project:
    record = make_project(user)
    db.add(record)
    await db.flush()
    return record


@pytest.fixture
def auth_headers(user):
    """Bearer header for `user`. Routes resolve the user from the token's sub."""
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token({'sub': user.email})}"}
