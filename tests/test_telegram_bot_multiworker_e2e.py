"""Opt-in real Telegram + PostgreSQL + THREE separate worker processes.

The process workers share only a real PostgreSQL server and Bot channel:
each opens a distinct DB connection and uploads through the real Bot API.
The production SQL throttle serializes Telegram send starts across workers.
Only this test's owned Telegram message IDs are cleaned up.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotApiClient, BotApiChunkTransport, BotFileIndex, _bot_files
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore


WORKER = r"""
import asyncio, os, sys
from pathlib import Path
from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotApiClient, BotApiChunkTransport, BotFileIndex
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore

async def run():
    db_url, source, ready, go, chat, account = sys.argv[1:]
    db = connect_database(db_url)
    try:
        transport = BotApiChunkTransport(
            BotApiClient(os.environ["TELEDRIVE_E2E_BOT_TOKEN"]),
            BotFileIndex(db), chat_id=int(chat), account=account,
            min_upload_interval=1.25,
        )
        Path(ready).write_text("ready", encoding="ascii")
        for _ in range(500):
            if Path(go).exists():
                break
            await asyncio.sleep(0.03)
        else:
            raise RuntimeError("Worker synchronization expired")
        manager = StorageUploadJobs(BucketCatalog(ManifestStore(db)))
        for _ in range(12):
            work = await manager.run_next(
                transport, allowed_root=Path(source),
                scratch_directory=Path(source), chunk_size=256 * 1024,
            )
            if work is None:
                return
            if work["state"] != "done":
                raise RuntimeError("Live multiworker upload was not committed")
        raise RuntimeError("Worker claimed too many jobs")
    finally:
        db.close()

asyncio.run(run())
"""


@pytest.mark.skipif(
    os.getenv("TELEDRIVE_E2E_MULTIWORKER") != "1",
    reason="Real PostgreSQL and Bot multiworker soak requires explicit opt-in",
)
def test_live_three_worker_pg_bot_soak_with_remote_checksum_cleanup():
    url = os.getenv("DB_TEST_POSTGRES_URL", "")
    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat_str = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not (url and token and chat_str):
        pytest.fail("Opt-in multiworker Telegram E2E needs PG and Bot credentials")
    chat = int(chat_str)
    with tempfile.TemporaryDirectory(prefix="teledrive-pg-bot-soak-") as folder:
        root = Path(folder)
        database = connect_database(url)
        account = "bot-ci-multi-" + uuid4().hex[:12]
        bucket = "ci-pg-" + uuid4().hex[:16]
        sizes = (0, 1, 65537, 262159, 1024 * 1024 + 11,
                 2 * 1024 * 1024 + 9)
        expected = {}
        processes = []
        bot = BotApiClient(token)
        try:
            catalog = BucketCatalog(ManifestStore(database))
            catalog.create_bucket(bucket, int(time.time()))
            BotFileIndex(database)  # create SQL slots before workers race
            queue = StorageUploadJobs(catalog)
            for n, size in enumerate(sizes):
                data = (bytes((0, 255)) * ((size + 1)//2))[:size] if n % 2 == 0 else os.urandom(size)
                filename = root / f"source-{n}.bin"
                filename.write_bytes(data)
                key = f"multiworker/{n}-{size}.bin"
                queue.enqueue(bucket, key, filename, allowed_root=root)
                expected[key] = data
            assert len(queue.list_jobs(limit=50)) >= len(sizes)
            go = root / "all-workers-start"
            for n in range(3):
                ready = root / f"worker-{n}.ready"
                worker = subprocess.Popen(
                    [sys.executable, "-c", WORKER, url, str(root),
                     str(ready), str(go), str(chat), account],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=Path(__file__).resolve().parents[1],
                )
                processes.append((worker, ready))
            deadline = time.monotonic() + 40
            while not all(ready.exists() for _, ready in processes):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Three SQL worker processes did not start")
                if any(proc.poll() is not None for proc, _ in processes):
                    raise RuntimeError("Real Bot worker failed before the start gate")
                time.sleep(0.05)
            go.write_text("go", encoding="ascii")
            for proc, _ in processes:
                stdout, stderr = proc.communicate(timeout=480)
                assert proc.returncode == 0, stderr.decode(errors="replace")[-2000:]
            database.close()
            database = connect_database(url)
            catalog = BucketCatalog(ManifestStore(database))
            jobs = [
                entry for entry in StorageUploadJobs(catalog).list_jobs(limit=1000)
                if entry["bucket"] == bucket
            ]
            assert len(jobs) == len(sizes)
            assert all(row["state"] == "done" and row["attempts"] == 1 for row in jobs)
            transport = BotApiChunkTransport(
                bot, BotFileIndex(database), chat_id=chat, account=account,
                min_upload_interval=0,
            )
            engine = TelegramObjectStorage(
                catalog.manifests, transport, scratch_directory=root,
            )
            for key, body in expected.items():
                item = catalog.get(bucket, key).item
                assert item.sha256 == hashlib.sha256(body).hexdigest()
                destination = root / f"restored-{uuid4().hex}.bin"
                asyncio.run(engine.download_to_path(item.id, destination))
                assert destination.read_bytes() == body
                received = asyncio.run(_collect_range(
                    engine, item.id, len(body), len(body)//2,
                ))
                assert received == body[len(body)//2:min(len(body), len(body)//2+37)]
            assert BotFileIndex(database).unresolved_count() == 0
        finally:
            for proc, _ in processes:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=10)
            # Only the globally unique account alias created by this run.
            try:
                owned = database.connection.execute(
                    select(_bot_files.c.message_id).where(
                        _bot_files.c.account == account,
                        _bot_files.c.chat_id == chat,
                    )
                ).scalars().all()
                for mid in sorted(set(owned)):
                    asyncio.run(bot.call("deleteMessage", {
                        "chat_id": chat, "message_id": int(mid),
                    }))
            finally:
                database.close()


async def _collect_range(engine, ident, size, start):
    return b"".join([
        part async for part in engine.iter_bytes(
            ident, start=start, end=min(size, start+37),
            block_size=7,
        )
    ])
