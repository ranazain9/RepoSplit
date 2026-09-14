"""Canonical hashing helpers shared by ingest and the Governance agent."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".reposplit",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".db", ".log"}


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def iter_repo_files(root: Path, suffixes: Iterable[str] | None = None) -> list[Path]:
    """Deterministically ordered list of files under `root`, skipping vendored/cache dirs."""
    wanted = set(suffixes) if suffixes else None
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        if wanted and path.suffix not in wanted:
            continue
        files.append(path)
    return files


def hash_tree(root: Path) -> str:
    """Merkle-ish digest: sha256 over sorted 'relpath\\0sha256(content)\\n' lines."""
    h = hashlib.sha256()
    for path in iter_repo_files(root):
        rel = path.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_hex(path.read_bytes()).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def hash_file(path: Path) -> str:
    return sha256_hex(path.read_bytes())
