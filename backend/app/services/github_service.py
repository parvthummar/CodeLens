import asyncio
import shutil
import pathlib

async def clone_repo(repo_url: str, dest_dir: str) -> None:
    process = await asyncio.create_subprocess_exec(
        'git', 'clone', '--depth', '1', repo_url, dest_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"Git clone failed: {stderr.decode()}")

def cleanup_repo(dest_dir: str) -> None:
    shutil.rmtree(dest_dir, ignore_errors=True)
