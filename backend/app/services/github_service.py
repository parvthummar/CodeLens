import asyncio
import shutil
import subprocess


async def clone_repo(repo_url: str, dest_dir: str) -> None:
    """Shallow-clone a git repo. Uses subprocess.run in a thread for Windows compatibility."""

    def _clone():
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, dest_dir],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Git clone failed: {result.stderr}")

    await asyncio.to_thread(_clone)


async def head_commit(dest_dir: str) -> str | None:
    """The SHA a clone actually landed on, or None if it cannot be determined.

    Deliberately not fatal. This is provenance — "which commit is in the index" —
    not something the pipeline needs in order to work, so a repository that
    somehow has no resolvable HEAD should still index rather than fail the run.
    """

    def _rev_parse() -> str | None:
        result = subprocess.run(
            ["git", "-C", dest_dir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    return await asyncio.to_thread(_rev_parse)


def cleanup_repo(dest_dir: str) -> None:
    shutil.rmtree(dest_dir, ignore_errors=True)
