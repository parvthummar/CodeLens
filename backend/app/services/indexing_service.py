import tempfile
import traceback
import uuid

from sqlalchemy import select, update

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

# Rows per INSERT ... ON CONFLICT, each committed on its own so a failure late
# in a large repo does not discard the work already persisted.
_DB_BATCH = 500


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


async def _set_status(
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


async def _persist_entities(
    project_id: uuid.UUID,
    entities: list[parser_service.CodeEntity],
    descriptions: list[str],
) -> tuple[dict[tuple[str, str], int], list[int]]:
    """Write entities to Postgres.

    Returns the id assigned to each entity keyed by (file_path, qualname), plus
    the ids of entities that existed before this run and no longer do.
    """
    rows = entity_service.build_rows(project_id, entities, descriptions)

    id_by_key: dict[tuple[str, str], int] = {}
    for start in range(0, len(rows), _DB_BATCH):
        async with session_scope() as db:
            id_by_key.update(
                await entity_service.upsert_entities(db, rows[start : start + _DB_BATCH])
            )
            await db.commit()

    async with session_scope() as db:
        removed_ids = await entity_service.delete_missing(
            db, project_id, list(id_by_key.values())
        )
        await db.commit()

    return id_by_key, removed_ids


async def run_indexing_pipeline(project_id: str) -> None:
    pid = uuid.UUID(project_id)
    dest_dir = None
    try:
        loaded = await _load_project(pid)
        if loaded is None:
            return
        repo_url, namespace = loaded

        await _set_status(pid, ProjectStatus.CLONING)

        dest_dir = tempfile.mkdtemp()
        await github_service.clone_repo(repo_url, dest_dir)

        await _set_status(pid, ProjectStatus.INDEXING)

        # Collapse duplicate definitions before anything expensive happens, so
        # we neither describe nor embed a row that will be discarded.
        entities = entity_service.dedupe(parser_service.parse_codebase(dest_dir))

        if entities:
            descriptions = await llm_service.generate_descriptions_batch(entities)
            embeddings = await embedding_service.embed_texts(descriptions)
        else:
            descriptions, embeddings = [], []

        # Postgres first: the vector ID *is* the row ID, so the rows have to
        # exist before there is anything to key vectors on. A crash between the
        # two leaves rows without vectors, which search tolerates by skipping
        # ids it cannot hydrate.
        id_by_key, removed_ids = await _persist_entities(pid, entities, descriptions)

        vectors = [
            (
                str(id_by_key[(entity.file_path, entity.name)]),
                embeddings[i],
                # Metadata stays minimal — the namespace already scopes by
                # project and everything else is hydrated from Postgres.
                # entity_type is kept so Pinecone can filter on it later.
                {"entity_type": entity.entity_type},
            )
            for i, entity in enumerate(entities)
        ]

        await pinecone_service.upsert_vectors(namespace, vectors)
        if removed_ids:
            await pinecone_service.delete_vectors(
                namespace, [str(i) for i in removed_ids]
            )

        await _set_status(pid, ProjectStatus.READY)

    except Exception as e:
        print(f"[INDEXING ERROR] project={project_id}: {repr(e)}")
        traceback.print_exc()
        try:
            await _set_status(pid, ProjectStatus.FAILED, error_message=repr(e))
        except Exception:
            # Don't let a failure recording the failure mask the original.
            traceback.print_exc()
    finally:
        if dest_dir:
            github_service.cleanup_repo(dest_dir)
