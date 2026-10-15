"""Authenticated legacy-folder importer must dry-run before enqueuing."""
from __future__ import annotations

import time
import pytest
from pathlib import Path
from types import SimpleNamespace

from tests.s3.test_s3_admin import admin
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore
import web.app as webapp


AUTH = ("testadmin", "secure-auth-password")
CSRF = {"x-teledrive-csrf": "1"}
URL = "/api/v1/admin/storage/jobs/import-legacy"


def test_admin_preview_and_execute_legacy_import_without_telegram(admin, tmp_path, monkeypatch):
    client, db_path, _ = admin
    root = tmp_path / "uploads"
    root.mkdir()
    folder = root / "legacy"
    folder.mkdir()
    (folder / "first.txt").write_bytes(b"first")
    (folder / "skip.dat").write_bytes(b"excluded")
    monkeypatch.setenv("TELEDRIVE_JOB_SOURCE_ROOT", str(root))
    cfg = SimpleNamespace(source_dir=str(folder), allowed_extensions=[".txt"],
                          max_file_size_mb=1)
    monkeypatch.setattr(webapp, "effective_config", lambda: cfg)
    db = connect_database(db_path)
    try:
        BucketCatalog(ManifestStore(db)).create_bucket("import-ci", int(time.time()))
    finally:
        db.close()
    body = {"bucket": "import-ci", "prefix": "legacy", "max_files": 10}
    assert client.post(URL, json=body).status_code in (401, 403)
    assert client.post(URL, auth=AUTH, json=body).status_code == 403

    preview = client.post(URL, auth=AUTH, headers=CSRF, json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    assert preview.json()["would_queue"] == 1
    assert preview.json()["queued"] == 0
    assert preview.json()["uploads_started"] == 0
    assert client.get("/api/v1/admin/storage/jobs", auth=AUTH).json()["jobs"] == []

    executed = client.post(URL, auth=AUTH, headers=CSRF,
                           json={**body, "execute": True})
    assert executed.status_code == 200, executed.text
    assert executed.json()["queued"] == executed.json()["would_queue"] == 1
    assert executed.json()["excluded"] == 1
    jobs = client.get("/api/v1/admin/storage/jobs", auth=AUTH).json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["object_key"] == "legacy/first.txt"
    assert jobs[0]["state"] == "queued"

    repeated = client.post(URL, auth=AUTH, headers=CSRF,
                           json={**body, "execute": True})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["queued"] == 0
    assert repeated.json()["already_queued"] == 1
    assert len(client.get("/api/v1/admin/storage/jobs", auth=AUTH).json()["jobs"]) == 1


def test_admin_legacy_import_refuses_external_source_and_active_legacy_uploader(
    admin, tmp_path, monkeypatch
):
    client, db_path, _ = admin
    approved = tmp_path / "approved"
    approved.mkdir()
    external = tmp_path / "outside"
    external.mkdir()
    (external / "a.txt").write_bytes(b"no-read")
    monkeypatch.setenv("TELEDRIVE_JOB_SOURCE_ROOT", str(approved))
    monkeypatch.setattr(webapp, "effective_config", lambda: SimpleNamespace(
        source_dir=str(external), allowed_extensions=[], max_file_size_mb=0,
    ))
    db = connect_database(db_path)
    try:
        BucketCatalog(ManifestStore(db)).create_bucket("import-ci", int(time.time()))
    finally:
        db.close()
    body = {"bucket": "import-ci", "execute": True}
    refused = client.post(URL, auth=AUTH, headers=CSRF, json=body)
    assert refused.status_code == 403
    assert len(client.get("/api/v1/admin/storage/jobs", auth=AUTH).json()["jobs"]) == 0

    (approved / "a.txt").write_bytes(b"approved")
    monkeypatch.setattr(webapp, "effective_config", lambda: SimpleNamespace(
        source_dir=str(approved), allowed_extensions=[], max_file_size_mb=0,
    ))
    lease = connect_database(db_path)
    try:
        from teledrive.uploader import LEASE_NAME
        assert lease.acquire_lease(LEASE_NAME, "another-live-uploader", int(time.time()), 120)
        refused = client.post(URL, auth=AUTH, headers=CSRF, json=body)
        assert refused.status_code == 409
        assert client.get("/api/v1/admin/storage/jobs", auth=AUTH).json()["jobs"] == []
    finally:
        lease.release_lease(LEASE_NAME, "another-live-uploader")
        lease.close()


def test_manual_legacy_and_job_uploads_blocked_during_automatic_cutover(
    admin, monkeypatch
):
    import asyncio
    from fastapi import HTTPException

    client, _, _ = admin
    monkeypatch.setenv("TELEDRIVE_JOB_LEGACY_AUTO_BUCKET", "migration-ci")
    monkeypatch.setenv("TELEDRIVE_JOB_AUTO_RUN", "1")
    denied = client.post(
        "/api/v1/admin/storage/jobs/run-next", auth=AUTH, headers=CSRF,
    )
    assert denied.status_code == 409, denied.text
    assert "cutover" in denied.text.lower()
    with pytest.raises(HTTPException) as error:
        asyncio.run(webapp.start_uploader("run-once"))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        asyncio.run(webapp.start_uploader("connect"))
    assert error.value.status_code == 409


def test_automatic_cutover_flag_validates_legacy_config_and_scan_limit(monkeypatch):
    import pytest

    monkeypatch.setenv("TELEDRIVE_JOB_LEGACY_AUTO_BUCKET", "migration-ci")
    monkeypatch.delenv("TELEDRIVE_JOB_AUTO_RUN", raising=False)
    monkeypatch.setattr(webapp, "read_raw_config", lambda: {"app": {"auto_run": False}})
    with pytest.raises(RuntimeError, match="AUTO_RUN"):
        webapp._legacy_cutover_configuration()
    monkeypatch.setenv("TELEDRIVE_JOB_AUTO_RUN", "1")
    monkeypatch.setattr(webapp, "read_raw_config", lambda: {"app": {"auto_run": True}})
    with pytest.raises(RuntimeError, match="Disable legacy"):
        webapp._legacy_cutover_configuration()
    monkeypatch.setattr(webapp, "read_raw_config", lambda: {"app": {"auto_run": False}})
    for limit in ("0", "1001", "not-an-integer"):
        monkeypatch.setenv("TELEDRIVE_JOB_LEGACY_AUTO_LIMIT", limit)
        with pytest.raises(RuntimeError):
            webapp._legacy_cutover_configuration()
    monkeypatch.setenv("TELEDRIVE_JOB_LEGACY_AUTO_LIMIT", "17")
    monkeypatch.setenv("TELEDRIVE_JOB_LEGACY_AUTO_PREFIX", "archive")
    assert webapp._legacy_cutover_configuration() == (
        "migration-ci", "archive", 17,
    )
