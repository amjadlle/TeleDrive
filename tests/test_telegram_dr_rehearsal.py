"""Read-only DR restores a SQL snapshot and downloads actual remote bytes.

Uses the production chunk engine plus a disk-backed Telegram stand-in,
and verifies that the archive and live SQL files are never modified.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time

import pytest

from scripts.backup_metadata import backup_sqlite
from scripts.rehearse_telegram_dr import verify_restored_telegram_object
from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotApiChunkTransport, BotFileIndex
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.manifest import ManifestStore


class FakeRealRemote:
    def __init__(self):
        self.documents = {}
        self.uploads = 0
        self.downloads = 0
        self.deletions = 0

    async def call(self, method, fields, source=None):
        if method == "sendDocument":
            self.uploads += 1
            name = "bot-file-" + str(self.uploads)
            self.documents[name] = source.read_bytes()
            return {
                "message_id": self.uploads,
                "chat": {"id": -10093744},
                "document": {"file_id": name},
            }
        if method == "getFile":
            file_id = fields["file_id"]
            return {"file_path": file_id, "file_size": len(self.documents[file_id])}
        if method == "deleteMessage":
            self.deletions += 1
            return {}
        raise AssertionError("Unexpected Bot API operation " + method)

    async def download_file(self, remote_path, destination):
        self.downloads += 1
        destination.write_bytes(self.documents[remote_path])


def seed(tmp_path, size):
    root = tmp_path / "live"
    root.mkdir()
    state = root / "manifest.db"
    source = root / "body.bin"
    payload = os.urandom(size)
    source.write_bytes(payload)
    remote = FakeRealRemote()
    db = connect_database(str(state))
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket("dr-test", int(time.time()))
        transport = BotApiChunkTransport(
            remote, BotFileIndex(db), account="dr-bot",
            chat_id=-10093744, min_upload_interval=0,
        )
        stored = asyncio.run(TelegramObjectStorage(
            catalog.manifests, transport, chunk_size=64 * 1024,
            scratch_directory=root,
        ).put_file("dr-test", "restore/sample.bin", source))
        catalog.publish(stored.id)
        assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    finally:
        db.close()
    archive = tmp_path / "sealed-archive.db"
    checksum = backup_sqlite(state, archive)
    return state, archive, checksum, remote, payload


@pytest.mark.parametrize("size", [0, 1, 65537, 262147])
def test_staging_dr_snapshot_and_bot_retrieval_are_byte_exact(tmp_path, size):
    state, archive, receipt, remote, payload = seed(tmp_path, size)
    live_before = hashlib.sha256(state.read_bytes()).hexdigest()
    archive_before = archive.read_bytes()
    uploads_before = remote.uploads
    report = asyncio.run(verify_restored_telegram_object(
        archive, receipt, remote, bucket="dr-test", key="restore/sample.bin",
        account="dr-bot", chat_id=-10093744,
    ))
    assert report["verified"] and report["archive_sha256"] == receipt
    assert report["sha256"] == hashlib.sha256(payload).hexdigest()
    assert report["size"] == report["readback_bytes"] == size
    assert report["telegram_uploads"] == report["telegram_deletions"] == 0
    assert report["source_database_modified"] is False
    assert archive.read_bytes() == archive_before
    assert hashlib.sha256(state.read_bytes()).hexdigest() == live_before
    assert remote.uploads == uploads_before and remote.deletions == 0
    assert remote.downloads >= (0 if size == 0 else 1)


@pytest.mark.parametrize("error", ["tampered-archive", "tampered-remote",
                                     "oversized-budget", "wrong-bucket"])
def test_staging_dr_denies_corrupt_and_unauthorized_inputs(tmp_path, error):
    state, archive, receipt, remote, payload = seed(tmp_path, 65537)
    uploads_before = remote.uploads
    kwargs = dict(bucket="dr-test", key="restore/sample.bin",
                  account="dr-bot", chat_id=-10093744)
    if error == "tampered-archive":
        with archive.open("ab") as file:
            file.write(b"altered")
    elif error == "tampered-remote":
        remote.documents["bot-file-1"] = b"modified" * 1024
    elif error == "oversized-budget":
        kwargs["max_bytes"] = 65536
    else:
        kwargs["bucket"] = "another-bucket"
    with pytest.raises((RuntimeError, ValueError, LookupError, FileNotFoundError)):
        asyncio.run(verify_restored_telegram_object(
            archive, receipt, remote, **kwargs,
        ))
    assert remote.uploads == uploads_before and remote.deletions == 0
    assert state.is_file()
