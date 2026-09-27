"""Ingestion endpoints: remote Git repository clone and secure Zip upload."""

from __future__ import annotations

import subprocess
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

from reposplit.api.config import settings

router = APIRouter(prefix="/api", tags=["ingestion"])


def is_git_url(path: str) -> bool:
    cleaned = path.strip()
    return any(cleaned.startswith(p) for p in ("http://", "https://", "git@", "ssh://")) or cleaned.endswith(".git")


def _clone_git_repo(url: str, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1", url, str(target_dir)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            raise HTTPException(400, f"git clone failed: {err}")
        return target_dir
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "git clone timed out after 120 seconds") from None
    except FileNotFoundError:
        raise HTTPException(500, "git executable not found on server host") from None


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    resolved_target = target_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.infolist():
            filename = member.filename
            if filename.startswith("/") or ".." in Path(filename).parts:
                raise HTTPException(400, f"Potentially malicious zip entry: {filename}")
            dest_file = (target_dir / filename).resolve()
            if not str(dest_file).startswith(str(resolved_target)):
                raise HTTPException(400, f"Zip Slip attempt detected: {filename}")
            z.extract(member, target_dir)
            if not member.is_dir():
                count += 1
    return count


@router.post("/upload")
async def upload_repo(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files are supported for upload")

    upload_id = uuid.uuid4().hex[:12]
    upload_dir = settings.storage_dir / "uploads" / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    zip_path = upload_dir / "repo.zip"

    content = await file.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(413, f"Uploaded zip exceeds maximum allowed size of {settings.max_upload_size_mb} MB")

    zip_path.write_bytes(content)

    extracted_dir = upload_dir / "source"
    file_count = _safe_extract_zip(zip_path, extracted_dir)

    # Detect single root directory in zip
    children = [c for c in extracted_dir.iterdir() if not c.name.startswith(".")]
    final_dir = children[0] if len(children) == 1 and children[0].is_dir() else extracted_dir

    return {
        "upload_id": upload_id,
        "repo_path": str(final_dir).replace("\\", "/"),
        "file_count": file_count,
        "filename": file.filename,
    }
