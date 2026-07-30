"""Async SQLAlchemy engine and session management for Neon Postgres."""

import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import settings

# Understood by libpq — and therefore present in the URL the Neon console hands
# out — but not by asyncpg, which forwards unrecognised query parameters to the
# server as settings and fails the connection.
_LIBPQ_ONLY_PARAMS = frozenset({"sslmode", "channel_binding"})


def normalize_dsn(url: str) -> tuple[str, bool]:
    """Turn a libpq-style Postgres URL into an asyncpg DSN.

    Rewrites the scheme for SQLAlchemy's asyncpg dialect and strips parameters
    asyncpg cannot accept. Returns the DSN and whether TLS was requested, since
    that has to be re-applied through connect_args instead.
    """
    parts = urlsplit(url)

    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"

    params = parse_qsl(parts.query, keep_blank_values=True)
    ssl_required = any(
        key == "sslmode" and value != "disable" for key, value in params
    )
    kept = [(k, v) for k, v in params if k not in _LIBPQ_ONLY_PARAMS]

    dsn = urlunsplit(
        (scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )
    return dsn, ssl_required


def _unique_statement_name() -> str:
    """Generate a never-reused prepared-statement name.

    The pooled Neon endpoint is PgBouncer in transaction mode, so consecutive
    statements on one SQLAlchemy connection can land on different backends. A
    reused name then collides as DuplicatePreparedStatementError, which surfaces
    intermittently and looks random.
    """
    return f"__asyncpg_{uuid.uuid4()}__"


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def is_pooled_host(dsn: str) -> bool:
    """Whether the DSN points at Neon's PgBouncer endpoint rather than the direct one."""
    return "-pooler." in dsn


def build_engine_from_url(url: str) -> AsyncEngine:
    """Build an engine, choosing pooling behaviour from the endpoint type.

    Both prepared-statement settings below are DBAPI arguments, not engine
    arguments — SQLAlchemy rejects them if passed to create_async_engine
    directly.
    """
    dsn, ssl_required = normalize_dsn(url)

    connect_args: dict[str, object] = {}
    if ssl_required:
        connect_args["ssl"] = "require"

    if is_pooled_host(dsn):
        # PgBouncer in transaction mode. asyncpg names prepared statements
        # sequentially per connection, so a connection handed to a new session
        # collides with names the previous one left behind. Unique names avoid
        # the collision; NullPool keeps statements from piling up server-side.
        # This is SQLAlchemy's documented PgBouncer configuration.
        connect_args["prepared_statement_cache_size"] = 0
        connect_args["prepared_statement_name_func"] = _unique_statement_name
        return create_async_engine(dsn, connect_args=connect_args, poolclass=NullPool)

    # Direct endpoint: we own the connections, so keep a small warm pool and let
    # prepared statements be cached. pre_ping covers free-tier compute
    # auto-suspending when idle, which otherwise fails the first query back.
    return create_async_engine(
        dsn,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=2,
    )


def _build_engine() -> AsyncEngine:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set — add it to backend/.env")
    return build_engine_from_url(settings.database_url)


def get_engine() -> AsyncEngine:
    """Lazily build the engine so importing this module never needs a live DB."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session."""
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Short-lived session for work outside a request.

    Background jobs must not hold a session across a long pipeline — that pins
    a pooled connection for the duration. Open one of these per write instead.
    """
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    """Close the connection pool. Called from the FastAPI lifespan shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
