import tempfile
import traceback
import uuid

from sqlalchemy import select, update

from app.db.postgres import session_scope
from app.models.project import Project, ProjectStatus
from app.services import (
    embedding_service,
    github_service,
    llm_service,
    parser_service,
    pinecone_service,
)


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

        entities = parser_service.parse_codebase(dest_dir)
        if not entities:
            await _set_status(pid, ProjectStatus.READY)
            return

        descriptions = await llm_service.generate_descriptions_batch(entities)
        embeddings = await embedding_service.embed_texts(descriptions)

        # Pinecone metadata limit is 40 960 bytes per vector.
        # Truncate source code to keep the total payload well under that.
        _MAX_CODE_BYTES = 20_000

        vectors = []
        for i, entity in enumerate(entities):
            vector_id = f"{project_id}_{i}"
            code = entity.source_code
            if len(code.encode("utf-8")) > _MAX_CODE_BYTES:
                code = code.encode("utf-8")[:_MAX_CODE_BYTES].decode("utf-8", errors="ignore") + "\n# … (truncated)"
            metadata = {
                "name": entity.name,
                "entity_type": entity.entity_type,
                "code": code,
                "signature": entity.signature,
                "description": descriptions[i],
                "file_path": entity.file_path,
                "start_line": entity.start_line,
                "end_line": entity.end_line
            }
            vectors.append((vector_id, embeddings[i], metadata))

        await pinecone_service.upsert_vectors(namespace, vectors)

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
