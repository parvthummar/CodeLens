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


def cleanup_repo(dest_dir: str) -> None:
    shutil.rmtree(dest_dir, ignore_errors=True)
