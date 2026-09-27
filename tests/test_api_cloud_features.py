"""Unit and integration tests for Cloud Ingestion & Delivery features.

Tests URL validation, zip upload with path traversal / Zip Slip protection,
run artifacts zip packaging and download, and CLI push functionality.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from reposplit.api.server import (
    _safe_extract_zip,
    create_app,
    is_git_url,
)


def test_is_git_url_detection():
    # Valid Git URLs
    assert is_git_url("https://github.com/org/repo")
    assert is_git_url("https://github.com/org/repo.git")
    assert is_git_url("http://gitlab.com/group/project.git")
    assert is_git_url("git@github.com:org/repo.git")
    assert is_git_url("git://example.com/repo.git")

    # Local paths or invalid formats
    assert not is_git_url("examples/shop_monolith")
    assert not is_git_url("/home/user/monolith")
    assert not is_git_url("C:\\projects\\app")
    assert not is_git_url("just-a-folder")


def test_safe_extract_zip_valid(tmp_path: Path):
    zip_path = tmp_path / "valid.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("app/main.py", "print('hello world')")
        z.writestr("app/models.py", "class User: pass")

    dest_dir = tmp_path / "extracted"
    count = _safe_extract_zip(zip_path, dest_dir)
    assert count == 2
    assert (dest_dir / "app" / "main.py").exists()
    assert (dest_dir / "app" / "models.py").exists()
    assert (dest_dir / "app" / "main.py").read_text() == "print('hello world')"


def test_safe_extract_zip_prevents_zip_slip(tmp_path: Path):
    bad_zip = tmp_path / "malicious.zip"
    with zipfile.ZipFile(bad_zip, "w") as z:
        z.writestr("../evil.py", "malicious payload")

    dest_dir = tmp_path / "safe_extract"
    with pytest.raises(HTTPException) as exc_info:
        _safe_extract_zip(bad_zip, dest_dir)

    assert exc_info.value.status_code == 400
    assert "malicious" in exc_info.value.detail.lower() or "traversal" in exc_info.value.detail.lower()


def test_api_upload_endpoint(tmp_path: Path):
    app = create_app()
    client = TestClient(app)

    # 1. Non-zip upload should fail with 400
    res_bad = client.post(
        "/api/upload",
        files={"file": ("test.txt", b"plain text", "text/plain")},
    )
    assert res_bad.status_code == 400
    assert "Only .zip files are supported" in res_bad.json()["detail"]

    # 2. Valid zip upload
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("monolith/app.py", "# monolith entrypoint")
        z.writestr("monolith/settings.py", "DEBUG = True")
    buf.seek(0)

    res_good = client.post(
        "/api/upload",
        files={"file": ("monolith.zip", buf.getvalue(), "application/zip")},
    )
    assert res_good.status_code == 200
    data = res_good.json()
    assert "upload_id" in data
    assert "repo_path" in data
    assert data["file_count"] == 2
    assert Path(data["repo_path"]).exists()
    assert (Path(data["repo_path"]) / "app.py").exists()


def test_api_download_endpoints(tmp_path: Path):
    app = create_app()
    client = TestClient(app)

    # 1. Unknown run download returns 404
    res_unknown = client.get("/api/runs/nonexistent-run-id/download")
    assert res_unknown.status_code == 404

    # 2. Test downloading from a run with output directory
    fake_out = tmp_path / "fake_out"
    fake_out.mkdir()
    (fake_out / "service_a.py").write_text("print('service a')")

    manager = app.state.manager
    from reposplit.core.schemas import RunConfig
    from reposplit.core.supervisor import Supervisor

    config = RunConfig(repo_path=str(tmp_path), output_dir=str(fake_out), provider="mock")
    sup = Supervisor(config)
    from reposplit.api.server import RunHandle

    manager.runs[sup.run_id] = RunHandle(supervisor=sup, task=None)

    res_dl = client.get(f"/api/runs/{sup.run_id}/download")
    assert res_dl.status_code == 200
    assert res_dl.headers["content-type"] == "application/zip"

    # Verify extracted content matches
    dl_zip = zipfile.ZipFile(io.BytesIO(res_dl.content))
    namelist = dl_zip.namelist()
    assert "service_a.py" in namelist
