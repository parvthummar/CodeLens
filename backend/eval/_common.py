"""Shared helpers for the retrieval evaluation scripts.

The corpus is CodeLens itself. Rather than cloning from GitHub — which would
index whatever happens to be on the default branch — the scripts copy the
git-tracked `.py` files from the working tree. That keeps the thing being
measured identical to the thing being read when the labels were written.
"""

import json
import os
import pathlib
import shutil
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
BACKEND = HERE.parent
REPO_ROOT = BACKEND.parent


def tracked_python_files() -> list[str]:
    """Every .py file a clone of this repo would contain.

    `git ls-files` rather than a directory walk: the walk picks up
    `backend/cr_venv/`, which is gitignored but very much on disk, and
    `parse_codebase` skips `venv`/`.venv` but not `cr_venv`.
    """
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    return out.stdout.split()


def materialise_corpus(dest: str) -> int:
    """Copy the tracked .py files into `dest`, preserving repo-relative paths."""
    count = 0
    for rel in tracked_python_files():
        target = pathlib.Path(dest) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, target)
        count += 1
    return count


def key(file_path: str, qualname: str) -> str:
    """Canonical `path::qualname`, separator-normalised."""
    return f"{file_path.replace(os.sep, '/')}::{qualname}"


def load_golden_set() -> dict:
    return json.loads((HERE / "golden_set.json").read_text(encoding="utf-8"))


def matches(stored_key: str, expected: str) -> bool:
    """Whether an indexed entity satisfies a golden-set label.

    Suffix comparison on the path, because the corpus root is not fixed: an
    entity indexed as `backend/app/core/security.py::hash_password` and one
    indexed as `app/core/security.py::hash_password` are the same entity, and
    the label should not have to know which rooting was used. The qualname must
    match exactly — `login` exists in two files and they are different answers.
    """
    stored_path, _, stored_name = stored_key.rpartition("::")
    want_path, _, want_name = expected.rpartition("::")
    if stored_name != want_name:
        return False
    return stored_path.endswith(want_path) or want_path.endswith(stored_path)
