"""The indexing pipeline.

Runs in the worker process, never in the API. Two properties matter here:

**It is streamed, not staged.** The previous shape was
`parse -> describe all -> embed all -> write all`, which held one coroutine and
one 1024-float vector per entity for the whole run. Peak memory therefore grew
with repo size, and a failure anywhere discarded everything. Now entities flow
through describe -> embed -> write -> upsert in chunks, so peak memory is
bounded by the chunk size and each chunk is durable before the next one starts.

**It raises.** Deciding what a failure means — retry, back off, give up — is the
job of the worker and `job_service`, not of the pipeline. This used to swallow
every exception and mark the project failed, which made a transient rate limit
indistinguishable from a repo that will never parse.
"""

import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select, update

from app.config import settings
from app.db.postgres import session_scope
from app.models.project import Project, ProjectStatus
from app.services import (
    embedding_service,
    entity_service,
    github_service,
    llm_service,
    parser_service,
    pinecone_service,
)

# Rows per INSERT ... ON CONFLICT. A chunk is normally smaller than this, but a
# deployment that raises indexing_chunk_size should not turn into one enormous
# statement.
_DB_BATCH = 500

ProgressCallback = Callable[[int, int], Awaitable[None]]


@dataclass
class IndexingResult:
    entities_indexed: int
    entities_removed: int
    chunks: int


async def _load_project(project_id: uuid.UUID) -> tuple[str, str] | None:
    """Fetch the fields the pipeline needs, as plain values.

    Returns (github_repo_url, pinecone_namespace), or None if the project is
    gone. Deliberately not an ORM instance: the session closes on return, and a
    detached instance would raise the moment an attribute was touched.
    """
    async with session_scope() as db:
        row = (
            await db.execute(
                select(Project.github_repo_url, Project.pinecone_namespace).where(
                    Project.id == project_id
                )
            )
        ).one_or_none()
    return (row[0], row[1]) if row else None


async def set_project_status(
    project_id: uuid.UUID,
    status: ProjectStatus,
    *,
    error_message: str | None = None,
) -> None:
    """Write one status transition through its own short-lived session.

    The pipeline runs for minutes; holding a session open across it would pin a
    pooled connection for the duration. error_message is always written so a
    later success clears an earlier failure. updated_at is left to the column's
    onupdate default.
    """
    async with session_scope() as db:
        await db.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(status=status, error_message=error_message)
        )
        await db.commit()


async def _persist_chunk(
    project_id: uuid.UUID,
    entities: list[parser_service.CodeEntity],
    descriptions: list[str],
) -> dict[tuple[str, str], int]:
    """Write one chunk's entities, returning their ids keyed by identity."""
    rows = entity_service.build_rows(project_id, entities, descriptions)

    id_by_key: dict[tuple[str, str], int] = {}
    for start in range(0, len(rows), _DB_BATCH):
        async with session_scope() as db:
            id_by_key.update(
                await entity_service.upsert_entities(db, rows[start : start + _DB_BATCH])
            )
            await db.commit()
    return id_by_key


async def _drop_vanished(
    project_id: uuid.UUID, namespace: str, keep_ids: list[int]
) -> list[int]:
    """Remove entities that no longer exist in the repo, and their vectors."""
    async with session_scope() as db:
        removed_ids = await entity_service.delete_missing(db, project_id, keep_ids)
        await db.commit()

    if removed_ids:
        await pinecone_service.delete_vectors(
            namespace, [str(i) for i in removed_ids]
        )
    return removed_ids


def _chunks(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def run_indexing_pipeline(
    project_id: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> IndexingResult | None:
    """Clone, parse, describe, embed and store one repository.

    Returns None if the project no longer exists — a deleted project is not a
    failure. Raises on anything else; the caller decides whether to retry.
    """
    pid = uuid.UUID(project_id)
    dest_dir = None
    try:
        loaded = await _load_project(pid)
        if loaded is None:
            return None
        repo_url, namespace = loaded

        await set_project_status(pid, ProjectStatus.CLONING)

        dest_dir = tempfile.mkdtemp()
        await github_service.clone_repo(repo_url, dest_dir)

        await set_project_status(pid, ProjectStatus.INDEXING)

        # Collapse duplicate definitions before anything expensive happens, so
        # we neither describe nor embed a row that will be discarded.
        entities = entity_service.dedupe(parser_service.parse_codebase(dest_dir))
        total = len(entities)
        if on_progress:
            await on_progress(0, total)

        id_by_key: dict[tuple[str, str], int] = {}
        chunks = _chunks(entities, settings.indexing_chunk_size)

        for chunk in chunks:
            descriptions = await llm_service.generate_descriptions_batch(
                chunk, batch_size=settings.llm_concurrency
            )
            embeddings = await embedding_service.embed_texts(descriptions)

            # Postgres first: the vector ID *is* the row ID, so the rows have to
            # exist before there is anything to key vectors on. A crash between
            # the two leaves rows without vectors, which search tolerates by
            # skipping ids it cannot hydrate.
            chunk_ids = await _persist_chunk(pid, chunk, descriptions)
            id_by_key.update(chunk_ids)

            await pinecone_service.upsert_vectors(
                namespace,
                [
                    (
                        str(chunk_ids[(entity.file_path, entity.name)]),
                        embeddings[i],
                        # Metadata stays minimal — the namespace already scopes
                        # by project and everything else is hydrated from
                        # Postgres. entity_type is kept so Pinecone can filter
                        # on it later.
                        {"entity_type": entity.entity_type},
                    )
                    for i, entity in enumerate(chunk)
                ],
            )

            # Checkpoint only after both stores have the chunk, so recorded
            # progress never overstates what actually landed.
            if on_progress:
                await on_progress(len(id_by_key), total)

        # Deletion runs once, at the end: an entity is only "missing" relative
        # to the complete run, and doing this per chunk would delete everything
        # the later chunks were about to write.
        removed_ids = await _drop_vanished(pid, namespace, list(id_by_key.values()))

        await set_project_status(pid, ProjectStatus.READY)

        return IndexingResult(
            entities_indexed=len(id_by_key),
            entities_removed=len(removed_ids),
            chunks=len(chunks),
        )
    finally:
        if dest_dir:
            github_service.cleanup_repo(dest_dir)
