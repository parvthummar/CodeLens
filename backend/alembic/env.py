import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection

from alembic import context

# `prepend_sys_path = .` in alembic.ini makes the app importable when alembic is
# run from the backend/ directory.
from app.config import settings
from app.db.postgres import build_engine_from_url, normalize_dsn
from app.models.base import Base

# Importing the package registers every model on Base.metadata, which is what
# autogenerate diffs against. Without it, migrations come out empty.
import app.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _migration_url() -> str:
    """The URL migrations run against.

    Prefers the direct endpoint: DDL through Neon's transaction pooler is
    unreliable, and PgBouncer also hides the session state some migrations need.
    """
    url = settings.database_url_direct or settings.database_url
    if not url:
        raise RuntimeError(
            "Neither DATABASE_URL_DIRECT nor DATABASE_URL is set in backend/.env"
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`)."""
    dsn, _ = normalize_dsn(_migration_url())
    context.configure(
        url=dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Reuse the app's engine builder so URL normalisation, TLS and the
    # pooled/direct distinction stay defined in exactly one place.
    connectable = build_engine_from_url(_migration_url())

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
