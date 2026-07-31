"""Persistence for parsed code entities.

Postgres owns the entity records; Pinecone holds only their vectors, keyed on
`entities.id`. Keeping identity here is what lets a re-index become an upsert
plus a targeted delete instead of wiping the namespace.

It is also what makes re-indexing *incremental*. `content_hash` has existed
since the migration but nothing consumed it; `diff_entities` now does. A run
compares what the parser found against what the project already has and sorts
each entity into one of three buckets — described below on `EntityDiff` — of
which only the first costs money.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import BigInteger, bindparam, delete, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity import Entity
from app.services.parser_service import CodeEntity

# (file_path, qualname) — an entity's identity within a project.
EntityKey = tuple[str, str]

_CONSTRAINT = "uq_entities_project_file_qualname"

# The two maintenance updates below target the table rather than the mapped
# class. An UPDATE issued against the ORM entity with bound parameters is read
# as a bulk-update-by-primary-key and demands an `id` in every parameter set,
# which neither of them has: one matches on an array, the other on a bindparam.
# Core statements also skip identity-map synchronisation, which nothing here
# needs — no caller reads these rows back through the session.
_TABLE = Entity.__table__


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


@dataclass(frozen=True)
class StoredEntity:
    """The columns needed to decide what a re-index has to redo."""

    id: int
    content_hash: str
    start_line: int | None
    end_line: int | None


@dataclass
class EntityDiff:
    """What one run has to do, split by cost.

    - `changed` — new, or the source text differs. Needs a fresh description, a
      fresh embedding and a write. This is the only bucket that costs money.
    - `moved` — byte-identical source at different line numbers, because
      something above it changed. The description and vector are still correct;
      only the location is stale, and refreshing it is one UPDATE with no API
      call. Skipping this bucket entirely would leave the UI pointing at the
      wrong lines, which is why `content_hash` covers the source alone.
    - `unchanged_ids` — nothing to do but record that the run still saw them.
    """

    changed: list[CodeEntity] = field(default_factory=list)
    moved: list[tuple[int, CodeEntity]] = field(default_factory=list)
    unchanged_ids: list[int] = field(default_factory=list)

    @property
    def reused(self) -> int:
        """Entities that did not need the LLM or the embedding model."""
        return len(self.moved) + len(self.unchanged_ids)


def diff_entities(
    parsed: list[CodeEntity], stored: dict[EntityKey, StoredEntity]
) -> EntityDiff:
    """Sort freshly parsed entities against what the project already holds.

    Pure, so the expensive decision in the pipeline is testable without a
    database. `stored` being empty makes everything `changed`, which is exactly
    what a first index should do.
    """
    diff = EntityDiff()
    for entity in parsed:
        previous = stored.get((entity.file_path, entity.name))
        if previous is None or previous.content_hash != entity.content_hash:
            diff.changed.append(entity)
        elif (previous.start_line, previous.end_line) != (
            entity.start_line,
            entity.end_line,
        ):
            diff.moved.append((previous.id, entity))
        else:
            diff.unchanged_ids.append(previous.id)
    return diff


async def load_stored(
    db: AsyncSession, project_id: uuid.UUID
) -> dict[EntityKey, StoredEntity]:
    """Read one project's current entity identities and hashes.

    Four narrow columns rather than whole ORM objects: this loads a row per
    entity in the repository, and `source_code` is the large one.
    """
    rows = await db.execute(
        select(
            Entity.id,
            Entity.file_path,
            Entity.qualname,
            Entity.content_hash,
            Entity.start_line,
            Entity.end_line,
        ).where(Entity.project_id == project_id)
    )
    return {
        (file_path, qualname): StoredEntity(eid, content_hash, start_line, end_line)
        for eid, file_path, qualname, content_hash, start_line, end_line in rows.all()
    }


def build_rows(
    project_id: uuid.UUID,
    entities: list[CodeEntity],
    descriptions: list[str],
    *,
    run_id: uuid.UUID | None = None,
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
            "last_seen_run": run_id,
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
            "last_seen_run": stmt.excluded.last_seen_run,
            "updated_at": stmt.excluded.updated_at,
        },
    ).returning(Entity.id, Entity.file_path, Entity.qualname)

    result = await db.execute(stmt)
    return {(file_path, qualname): eid for eid, file_path, qualname in result.all()}


async def mark_seen(db: AsyncSession, ids: list[int], run_id: uuid.UUID) -> int:
    """Record that `run_id` still found these entities in the repository.

    `id = ANY(:ids)` rather than `IN (...)`: the array is a single bind
    parameter, so this is one statement whatever the entity count. An expanding
    IN list would reintroduce the parameter ceiling that the run marker exists
    to remove.

    `updated_at` is assigned to itself to suppress the mixin's onupdate. Nothing
    about these entities changed — only that a run looked at them — and letting
    the stamp move would make "when did this code last change" unanswerable
    after the first re-index.
    """
    if not ids:
        return 0
    result = await db.execute(
        update(_TABLE)
        .where(Entity.id == bindparam("seen_ids", value=ids, type_=ARRAY(BigInteger)).any_())
        .values(last_seen_run=run_id, updated_at=Entity.updated_at)
    )
    return result.rowcount


async def refresh_locations(
    db: AsyncSession, moved: list[tuple[int, CodeEntity]], run_id: uuid.UUID
) -> int:
    """Update line numbers for entities whose source is unchanged.

    The description and the vector still describe this code correctly — only
    where it sits in the file has changed — so this deliberately touches nothing
    else and makes no API call.
    """
    if not moved:
        return 0
    # One executemany rather than a statement per row.
    await db.execute(
        update(_TABLE)
        .where(Entity.id == bindparam("moved_id"))
        .values(
            start_line=bindparam("moved_start"),
            end_line=bindparam("moved_end"),
            last_seen_run=run_id,
        ),
        [
            {
                "moved_id": entity_id,
                "moved_start": entity.start_line,
                "moved_end": entity.end_line,
            }
            for entity_id, entity in moved
        ],
    )
    return len(moved)


async def delete_missing(
    db: AsyncSession, project_id: uuid.UUID, run_id: uuid.UUID
) -> list[int]:
    """Delete this project's entities that the given run did not see.

    Returns the removed ids so their vectors can be dropped from Pinecone. A run
    that found nothing deletes everything for the project, which is the correct
    outcome when a repo no longer parses to any entities.

    This used to take the ids to keep and put them in a NOT IN list, which made
    it bounded by the statement parameter limit — fine at hundreds of entities,
    a hard failure at tens of thousands. Every row the run keeps is stamped with
    `last_seen_run` as it passes through, so the delete now carries two
    parameters no matter how large the repository is. IS DISTINCT FROM rather
    than != because rows predating this column hold NULL, and a NULL marker
    means "not seen by this run", not "unknown".

    This relies on one project never being indexed by two runs at once — a
    second run's stamps would make the first one's rows look vanished. That
    invariant is `job_service.claim` plus the 409 on `/reindex`, not this
    statement, and it held for the NOT IN version too.
    """
    result = await db.execute(
        delete(Entity)
        .where(
            Entity.project_id == project_id,
            Entity.last_seen_run.is_distinct_from(run_id),
        )
        .returning(Entity.id)
    )
    return list(result.scalars().all())


async def get_by_ids(
    db: AsyncSession, project_id: uuid.UUID, ids: list[int]
) -> dict[int, Entity]:
    """Hydrate entities for search results, scoped to one project.

    project_id is required rather than optional: ids arrive from Pinecone, and
    scoping here means a stale or mismatched vector id can never surface another
    project's source. Namespaces already separate projects, so this is
    defence in depth.
    """
    if not ids:
        return {}
    result = await db.execute(
        select(Entity).where(Entity.project_id == project_id, Entity.id.in_(ids))
    )
    return {e.id: e for e in result.scalars().all()}


async def keyword_search(
    db: AsyncSession, project_id: uuid.UUID, query: str, limit: int = 10
) -> list[int]:
    """Rank this project's entities by full-text relevance. Returns ids, best first.

    `websearch_to_tsquery` rather than `to_tsquery`: it accepts whatever a user
    types. `to_tsquery` demands operator syntax and raises on a bare sentence,
    which would turn an ordinary query into a 500.

    `ts_rank_cd` rather than `ts_rank` — cover density, so terms appearing near
    each other score above the same terms scattered through a long function.
    Weighting is baked into the stored vector; see `SEARCH_VECTOR_SQL`.
    """
    if not query.strip():
        return []

    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank_cd(Entity.search_vector, tsquery)

    rows = await db.execute(
        select(Entity.id)
        .where(
            Entity.project_id == project_id,
            Entity.search_vector.op("@@")(tsquery),
        )
        .order_by(rank.desc(), Entity.id)
        .limit(limit)
    )
    return list(rows.scalars().all())


async def count_for_project(db: AsyncSession, project_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(Entity).where(Entity.project_id == project_id)
    )
    return result.scalar_one()
