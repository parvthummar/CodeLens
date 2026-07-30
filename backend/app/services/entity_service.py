"""Persistence for parsed code entities.

Postgres owns the entity records; Pinecone holds only their vectors, keyed on
`entities.id`. Keeping identity here is what lets a re-index become an upsert
plus a targeted delete instead of wiping the namespace.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity import Entity
from app.services.parser_service import CodeEntity

# (file_path, qualname) — an entity's identity within a project.
EntityKey = tuple[str, str]

_CONSTRAINT = "uq_entities_project_file_qualname"


def dedupe(entities: list[CodeEntity]) -> list[CodeEntity]:
    """Collapse entities sharing (file_path, qualname); the last one wins.

    Python allows a module-level name to be bound more than once — @overload
    stubs and plain redefinitions both do — and the unique constraint would
    reject such a batch. ON CONFLICT cannot rescue it either: Postgres refuses
    to let a single statement affect the same row twice.
    """
    by_key: dict[EntityKey, CodeEntity] = {}
    for e in entities:
        by_key[(e.file_path, e.name)] = e
    return list(by_key.values())


def build_rows(
    project_id: uuid.UUID,
    entities: list[CodeEntity],
    descriptions: list[str],
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Turn parsed entities plus their descriptions into insertable rows."""
    stamp = now or datetime.now(timezone.utc)
    return [
        {
            "project_id": project_id,
            "file_path": e.file_path,
            "qualname": e.name,
            "entity_type": e.entity_type,
            "signature": e.signature,
            "source_code": e.source_code,
            "description": description,
            "start_line": e.start_line,
            "end_line": e.end_line,
            "content_hash": e.content_hash,
            "created_at": stamp,
            "updated_at": stamp,
        }
        for e, description in zip(entities, descriptions)
    ]


async def upsert_entities(db: AsyncSession, rows: list[dict]) -> dict[EntityKey, int]:
    """Insert or update one batch, returning each row's id keyed by identity.

    RETURNING does not promise input order, so the mapping is built from the
    returned identity columns rather than by position.
    """
    if not rows:
        return {}

    stmt = pg_insert(Entity).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint=_CONSTRAINT,
        set_={
            "entity_type": stmt.excluded.entity_type,
            "signature": stmt.excluded.signature,
            "source_code": stmt.excluded.source_code,
            "description": stmt.excluded.description,
            "start_line": stmt.excluded.start_line,
            "end_line": stmt.excluded.end_line,
            "content_hash": stmt.excluded.content_hash,
            "updated_at": stmt.excluded.updated_at,
        },
    ).returning(Entity.id, Entity.file_path, Entity.qualname)

    result = await db.execute(stmt)
    return {(file_path, qualname): eid for eid, file_path, qualname in result.all()}


async def delete_missing(
    db: AsyncSession, project_id: uuid.UUID, keep_ids: list[int]
) -> list[int]:
    """Delete this project's entities that were not part of the current run.

    Returns the removed ids so their vectors can be dropped from Pinecone.
    Passing an empty keep_ids deletes everything for the project, which is the
    correct outcome when a repo no longer parses to any entities.

    Note: keep_ids goes into a NOT IN list, so this is bounded by the statement
    parameter limit. Fine at this scale; a per-run marker column would be the
    move if entity counts reach the tens of thousands.
    """
    stmt = delete(Entity).where(Entity.project_id == project_id)
    if keep_ids:
        stmt = stmt.where(Entity.id.notin_(keep_ids))
    result = await db.execute(stmt.returning(Entity.id))
    return list(result.scalars().all())


async def get_by_ids(db: AsyncSession, ids: list[int]) -> dict[int, Entity]:
    """Hydrate entities for search results."""
    if not ids:
        return {}
    result = await db.execute(select(Entity).where(Entity.id.in_(ids)))
    return {e.id: e for e in result.scalars().all()}


async def count_for_project(db: AsyncSession, project_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(Entity).where(Entity.project_id == project_id)
    )
    return result.scalar_one()
