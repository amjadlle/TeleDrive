"""Legacy folder -> durable object queue is explicit, idempotent and safe."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.jobs import StorageUploadJobs, jobs, UploadJobError
from teledrive.storage.legacy_bridge import enqueue_legacy_folder, run_legacy_cutover_cycle
from teledrive.storage.manifest import ManifestStore
from test_storage_engine import MemoryTelegram


@pytest.fixture
def bridge(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    db_path = tmp_path / "legacy-bridge.db"
    conn = connect_database(str(db_path))
    cat = BucketCatalog(ManifestStore(conn))
    cat.create_bucket("bridge-ci", int(time.time()))
    config = SimpleNamespace(
        source_dir=str(root), allowed_extensions=[".txt"], max_file_size_mb=1,
    )
    try:
        yield root, db_path, StorageUploadJobs(cat), config
    finally:
        conn.close()


def test_legacy_folder_bridge_idempotent_across_restart_and_preserves_paths(bridge):
    root, db_path, queue, cfg = bridge
    nested = root / "documents"
    nested.mkdir()
    (root / "one.txt").write_bytes(b"one")
    (nested / "two.txt").write_bytes(b"two")
    (root / "not-included.dat").write_bytes(b"exclude")
    (root / "external.txt").symlink_to(root / "one.txt")

    first = enqueue_legacy_folder(cfg, queue, bucket="bridge-ci", prefix="backup")
    assert first.queued == 2
    assert first.already_queued == 0
    assert first.excluded == 1
    assert len(first.job_ids) == 2
    records = queue.list_jobs()
    assert {r["object_key"] for r in records} == {
        "backup/one.txt", "backup/documents/two.txt",
    }
    assert all(r["state"] == "queued" for r in records)
    # No remote bytes are uploaded by a folder scan, even after restart.
    conn = connect_database(str(db_path))
    try:
        recreated = StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
        again = enqueue_legacy_folder(cfg, recreated, bucket="bridge-ci", prefix="backup")
        assert again.queued == 0 and again.already_queued == 2
        assert {r["id"] for r in recreated.list_jobs()} == set(first.job_ids)
        source = root / "one.txt"
        source.write_bytes(b"modified")
        changed = enqueue_legacy_folder(cfg, recreated, bucket="bridge-ci", prefix="backup")
        assert changed.queued == 1 and changed.already_queued == 1
        assert len(recreated.list_jobs()) == 3
    finally:
        conn.close()


def test_legacy_bridge_rejects_traversal_and_unknown_destination(bridge):
    root, _, queue, cfg = bridge
    (root / "normal.txt").write_bytes(b"no-send")
    for prefix in ("/absolute", "../escape", "foo/../bar", "foo//bar",
                   "bad\\windows", "bad/"):
        with pytest.raises(ValueError):
            enqueue_legacy_folder(cfg, queue, bucket="bridge-ci", prefix=prefix)
    for limit in (0, 1001):
        with pytest.raises(ValueError):
            enqueue_legacy_folder(cfg, queue, bucket="bridge-ci", max_files=limit)
    with pytest.raises(FileNotFoundError):
        enqueue_legacy_folder(cfg, queue, bucket="does-not-exist")
    assert queue.list_jobs() == []


def test_legacy_bridge_only_queues_with_explicit_worker(bridge):
    root, _, queue, cfg = bridge
    (root / "chunked.txt").write_bytes(bytes(range(30)))
    report = enqueue_legacy_folder(cfg, queue, bucket="bridge-ci", max_files=1)
    assert report.queued == 1
    remote = MemoryTelegram()
    assert remote.uploads == 0
    completed = asyncio.run(queue.run_next(
        remote, allowed_root=root, scratch_directory=root, chunk_size=7,
    ))
    assert completed["state"] == "done"
    assert remote.uploads == 5
    assert queue.catalog.get("bridge-ci", "chunked.txt").item.size == 30
    again = enqueue_legacy_folder(cfg, queue, bucket="bridge-ci")
    assert again.queued == 0 and again.already_queued == 1
    assert queue.list_jobs()[0]["state"] == "done"


def test_legacy_uploader_cli_bridge_never_connects_to_telegram(bridge, tmp_path):
    root, db_path, queue, cfg = bridge
    (root / "from-cli.txt").write_bytes(b"queued-not-sent")
    config_path = tmp_path / "legacy.yaml"
    config_path.write_text(yaml.safe_dump({
        "telegram": {
            "api_id": 12345, "api_hash": "test-not-secret",
            "target": "me", "session_path": str(tmp_path / "unused.session"),
        },
        "upload": {
            "source_dir": str(root), "allowed_extensions": [".txt"],
            "max_file_size_mb": 1, "sleep_min_seconds": 1,
            "sleep_max_seconds": 2, "max_files_per_run": 5,
            "max_files_per_day": 20,
        },
        "state": {"database_url": str(db_path)},
        "logging": {"log_path": str(tmp_path / "bridge.log"), "level": "INFO"},
    }), encoding="utf-8")
    args = [
        sys.executable, "-m", "teledrive.uploader",
        "--config", str(config_path), "--scan-only",
        "--queue-object-bucket", "bridge-ci",
        "--queue-object-prefix", "from-legacy",
    ]
    result = subprocess.run(args, cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "queued=1" in (tmp_path / "bridge.log").read_text()
    assert not (tmp_path / "unused.session").exists()
    assert {r["object_key"] for r in queue.list_jobs()} == {
        "from-legacy/from-cli.txt"
    }
    second = subprocess.run(args, cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stderr
    assert len(queue.list_jobs()) == 1
    unsafe = subprocess.run(args + ["--run-once"],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert unsafe.returncode != 0
    assert len(queue.list_jobs()) == 1


def _cutover(bridge, lease, *, bucket="bridge-ci", remote=None,
             allowed_root=None):
    root, _, queue, cfg = bridge
    return asyncio.run(run_legacy_cutover_cycle(
        cfg, queue, remote or MemoryTelegram(), lease_db=lease, bucket=bucket,
        allowed_root=allowed_root or root, scratch_directory=root,
        lease_name="uploader-global", lease_ttl=60, chunk_size=7,
    ))


def test_cutover_imports_once_and_uploads_exact_source_on_restart(bridge):
    root, path, queue, _ = bridge
    payload = b"verified-cutover-data" * 9
    (root / "cutover.txt").write_bytes(payload)
    remote = MemoryTelegram()
    lease = connect_database(str(path))
    try:
        first, result = _cutover(bridge, lease, remote=remote)
        assert first.queued == 1 and result["state"] == "done"
        before = remote.uploads
        second, again = _cutover(bridge, lease, remote=remote)
        assert second.queued == 0 and second.already_queued == 1
        assert again is None and remote.uploads == before
        assert len(queue.list_jobs()) == 1
        assert queue.catalog.get("bridge-ci", "cutover.txt").item.size == len(payload)
        assert lease.acquire_lease("uploader-global", "old-uploader", int(time.time()), 60)
        lease.release_lease("uploader-global", "old-uploader")
    finally:
        lease.close()


def test_cutover_refuses_concurrent_legacy_uploader_before_import(bridge):
    root, path, queue, _ = bridge
    (root / "blocked.txt").write_bytes(b"blocked")
    legacy = connect_database(str(path))
    lease = connect_database(str(path))
    try:
        assert legacy.acquire_lease("uploader-global", "legacy-sender", int(time.time()), 60)
        with pytest.raises(UploadJobError, match="holds"):
            _cutover(bridge, lease)
        assert queue.list_jobs() == []
        legacy.release_lease("uploader-global", "legacy-sender")
        imported, result = _cutover(bridge, lease)
        assert imported.queued == 1 and result["state"] == "done"
    finally:
        lease.close()
        legacy.close()


def test_cutover_checks_root_database_and_bucket_before_telegram(bridge, tmp_path):
    root, path, queue, _ = bridge
    (root / "private.txt").write_bytes(b"never-send-on-error")
    lease = connect_database(str(path))
    unrelated = connect_database(str(tmp_path / "unrelated.db"))
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        with pytest.raises(UploadJobError, match="approved job root"):
            _cutover(bridge, lease, allowed_root=outside)
        with pytest.raises(UploadJobError, match="same database"):
            _cutover(bridge, unrelated)
        with pytest.raises(FileNotFoundError):
            _cutover(bridge, lease, bucket="not-a-bucket")
        assert queue.list_jobs() == []
        assert lease.acquire_lease("uploader-global", "after-exception", int(time.time()), 60)
        lease.release_lease("uploader-global", "after-exception")
    finally:
        lease.close()
        unrelated.close()


def test_cutover_holds_legacy_lease_during_telegram_send(bridge):
    root, path, queue, _ = bridge
    (root / "hold.txt").write_bytes(b"lease-fenced-send")
    checker = connect_database(str(path))
    lease = connect_database(str(path))
    class FencedTelegram(MemoryTelegram):
        async def upload(self, path, *, object_id, ordinal, checksum):
            assert not checker.acquire_lease(
                "uploader-global", "racing-uploader", int(time.time()), 60
            )
            return await super().upload(
                path, object_id=object_id, ordinal=ordinal, checksum=checksum,
            )
    try:
        _, result = _cutover(bridge, lease, remote=FencedTelegram())
        assert result["state"] == "done"
        assert queue.list_jobs()[0]["attempts"] == 1
    finally:
        lease.close()
        checker.close()


def test_cutover_can_publish_after_delete_marker_with_head_cas(bridge):
    root, path, queue, _ = bridge
    (root / "after.txt").write_bytes(b"version-after-delete")
    marker = queue.catalog.delete_marker("bridge-ci", "after.txt")
    lease = connect_database(str(path))
    try:
        _, done = _cutover(bridge, lease)
        assert done["state"] == "done"
        assert queue.catalog.get("bridge-ci", "after.txt").item.id != marker
        versions, _ = queue.catalog.list_bucket_versions("bridge-ci")
        assert any(entry["version_id"] == marker and entry["is_delete_marker"]
                   for entry in versions)
    finally:
        lease.close()
