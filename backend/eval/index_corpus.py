"""Index the evaluation corpus for real. Spends money.

This is the one script here that makes paid calls: it runs the production
pipeline with nothing stubbed except the clone, so descriptions come from the
real LLM and vectors from the real embedding model and land in the real
Pinecone index. That is the point — an eval against stubbed retrieval would
measure nothing.

The clone is replaced by a copy of the working tree so the corpus matches the
code the golden set was written against, rather than whatever is on the default
branch. Every other stage is untouched production code.

    python -m eval.index_corpus            # index, printing the project id
    python -m eval.index_corpus --drop ID  # delete a corpus project and its vectors

Re-running is cheap rather than free: step 4's content-hash diff means a second
run over an unchanged tree makes zero LLM and zero embedding calls.
"""

import argparse
import asyncio
import shutil
import tempfile
import uuid

from app.core.security import hash_password
from app.db.postgres import dispose_engine, session_scope
from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.services import github_service, indexing_service, pinecone_service
from eval._common import REPO_ROOT, materialise_corpus

EVAL_EMAIL = "retrieval-eval@codelens.local"


async def _copy_instead_of_clone(url: str, dest: str) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    tmp = tempfile.mkdtemp()
    try:
        materialise_corpus(tmp)
        shutil.move(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


async def _head_commit(dest: str) -> str | None:
    """The working tree's HEAD, for provenance in the results file."""
    import subprocess

    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


async def eval_user_id() -> uuid.UUID:
    """A single reusable owner, so repeated runs do not litter the users table."""
    from sqlalchemy import select

    async with session_scope() as db:
        existing = (
            await db.execute(select(User.id).where(User.email == EVAL_EMAIL))
        ).scalar_one_or_none()
        if existing:
            return existing
        user = User(
            email=EVAL_EMAIL,
            hashed_password=hash_password(uuid.uuid4().hex),
            full_name="Retrieval eval",
        )
        db.add(user)
        await db.commit()
        return user.id


async def create() -> uuid.UUID:
    pid = uuid.uuid4()
    async with session_scope() as db:
        db.add(
            Project(
                id=pid,
                user_id=await eval_user_id(),
                name="codelens-eval-corpus",
                github_repo_url=str(REPO_ROOT),
                github_owner="parvthummar",
                github_repo_name="CodeLens",
                pinecone_namespace=str(pid),
                status=ProjectStatus.QUEUED,
            )
        )
        await db.commit()
    return pid


async def drop(project_id: str) -> None:
    from sqlalchemy import delete, select

    pid = uuid.UUID(project_id)
    async with session_scope() as db:
        namespace = (
            await db.execute(
                select(Project.pinecone_namespace).where(Project.id == pid)
            )
        ).scalar_one_or_none()
        if namespace is None:
            print(f"no such project {project_id}")
            return
        await db.execute(delete(Project).where(Project.id == pid))
        await db.commit()
    try:
        await pinecone_service.delete_namespace(namespace)
    except Exception as e:
        print(f"warning: Pinecone namespace {namespace} not deleted: {e!r}")
    print(f"dropped {project_id}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", metavar="PROJECT_ID")
    args = ap.parse_args()

    try:
        if args.drop:
            await drop(args.drop)
            return

        github_service.clone_repo = _copy_instead_of_clone
        github_service.head_commit = _head_commit

        pid = await create()
        print(f"project {pid}\nindexing (this makes real API calls)...")

        async def progress(done: int, total: int) -> None:
            print(f"  {done}/{total}")

        result = await indexing_service.run_indexing_pipeline(
            str(pid), on_progress=progress
        )
        print(
            f"\nindexed  {result.entities_indexed} entities "
            f"({result.entities_described} described, "
            f"{result.entities_reused} reused)"
        )
        print(f"commit   {result.commit_sha}")
        print(f"\nrun the eval with:\n  python -m eval.run_eval {pid}")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
