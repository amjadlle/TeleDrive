"""Opt-in real Telegram + PostgreSQL + THREE separate worker processes.

The process workers share only a real PostgreSQL server and a dedicated
Telethon USER session/channel: each opens a distinct SQL connection and its
own MTProto client. The production SQL rate guard serializes Telegram send
starts across workers. Only this test's owned Telegram message IDs are
cleaned up after byte-verified readback.
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
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore, chunks
from teledrive.storage.target_resolver import resolve_telegram_target
from teledrive.storage.telethon_adapter import TelethonChunkTransport
from teledrive.storage.user_mode import UserSendJournal


WORKER = r"""
import asyncio, os, sys, traceback
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore
from teledrive.storage.target_resolver import resolve_telegram_target
from teledrive.storage.telethon_adapter import TelethonChunkTransport
from teledrive.storage.user_mode import UnifiedUserTransport, UserSendJournal

async def run():
    db_url, source, ready, go, target, account = sys.argv[1:]
    Path(ready).write_text("ready", encoding="ascii")
    for _ in range(500):
        if Path(go).exists():
            break
        await asyncio.sleep(0.03)
    else:
        raise RuntimeError("Worker synchronization expired")
    db = connect_database(db_url)
    try:
        manager = StorageUploadJobs(BucketCatalog(ManifestStore(db)))
        channel_name = os.getenv("TELEDRIVE_E2E_CHANNEL_NAME") or None
        # One USER StringSession must never hold concurrent connections:
        # serialize the whole per-job connect/upload/disconnect window
        # across workers with a file lock in the shared source tree.
        import fcntl
        lock = open(Path(source) / "mtproto-connect.lock", "w")
        for _ in range(12):
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                # Each job uses a fresh, short-lived client.
                client = TelegramClient(
                    StringSession(os.environ["TELEDRIVE_E2E_SESSION"]),
                    int(os.environ["TELEDRIVE_E2E_API_ID"]),
                    os.environ["TELEDRIVE_E2E_API_HASH"],
                    device_model="TeleDrive Storage Client",
                    app_version="TeleDrive 2.1",
                    receive_updates=False, flood_sleep_threshold=60,
                )
                await client.connect()
                try:
                    resolved = await resolve_telegram_target(
                        client, target or None, channel_name,
                    )
                    peer_chat_id = int(await client.get_peer_id(resolved))
                    from types import SimpleNamespace
                    runtime = SimpleNamespace(
                        account=account, chat_id=peer_chat_id,
                    )
                    runtime.require = lambda: TelethonChunkTransport({
                        account: (client, resolved),
                    })

                    transport = UnifiedUserTransport(
                        runtime, UserSendJournal(db), min_upload_interval=1.25,
                    )
                    work = await manager.run_next(
                        transport, allowed_root=Path(source),
                        scratch_directory=Path(source), chunk_size=256 * 1024,
                    )
                    if work is None:
                        return
                    if work["state"] != "done":
                        raise RuntimeError("Live multiworker upload was not committed")
                finally:
                    await client.disconnect()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        raise RuntimeError("Worker claimed too many jobs")
    finally:
        db.close()

try:
    asyncio.run(run())
except BaseException as exc:
    print(f"WORKER-FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    raise SystemExit(1)
"""


@pytest.mark.skipif(
    os.getenv("TELEDRIVE_E2E_MULTIWORKER") != "1",
    reason="Real PostgreSQL and USER MTProto multiworker soak requires explicit opt-in",
)
def test_live_three_worker_user_mtproto_soak_with_remote_checksum_cleanup():
    url = os.getenv("DB_TEST_POSTGRES_URL", "")
    session = os.getenv("TELEDRIVE_E2E_SESSION", "")
    api_id = os.getenv("TELEDRIVE_E2E_API_ID", "")
    api_hash = os.getenv("TELEDRIVE_E2E_API_HASH", "")
    target = os.getenv("TELEDRIVE_E2E_TARGET", "")
    channel_name = os.getenv("TELEDRIVE_E2E_CHANNEL_NAME", "")
    if not (url and session and api_id and api_hash and (target or channel_name)):
        pytest.fail("Opt-in multiworker Telegram E2E needs PG and USER credentials")
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    with tempfile.TemporaryDirectory(prefix="teledrive-pg-user-soak-") as folder:
        root = Path(folder)
        database = connect_database(url)
        account = "user-ci-multi-" + uuid4().hex[:12]
        bucket = "ci-pg-" + uuid4().hex[:16]
        sizes = (0, 1, 65537, 262159, 1024 * 1024 + 11,
                 2 * 1024 * 1024 + 9)
        expected = {}
        processes = []
        try:
            catalog = BucketCatalog(ManifestStore(database))
            catalog.create_bucket(bucket, int(time.time()))
            UserSendJournal(database)  # create SQL intents/slots before workers race
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
                     str(ready), str(go), target, account],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=Path(__file__).resolve().parents[2],
                )
                processes.append((worker, ready))
            deadline = time.monotonic() + 60
            while not all(ready.exists() for _, ready in processes):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Three SQL worker processes did not start")
                if any(proc.poll() is not None for proc, _ in processes):
                    for proc, _ in processes:
                        if proc.poll() is not None:
                            _, stderr = proc.communicate(timeout=10)
                            raise RuntimeError(
                                "Real USER worker failed before the start gate: "
                                + stderr.decode(errors="replace")[-1500:]
                            )
                    raise RuntimeError("Real USER worker failed before the start gate")
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
            # Readback phase runs after all workers disconnected their
            # clients, so only this process holds the USER session now.
            client = TelegramClient(
                StringSession(session), int(api_id), api_hash,
                device_model="TeleDrive Storage Client",
                app_version="TeleDrive 2.1",
                receive_updates=False, flood_sleep_threshold=60,
            )
            asyncio.run(client.connect())
            try:
                resolved = asyncio.run(resolve_telegram_target(
                    client, target or None, channel_name or None,
                ))
                chat = int(asyncio.run(client.get_peer_id(resolved)))
                transport = TelethonChunkTransport({account: (client, resolved)})
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
                assert UserSendJournal(database).unresolved_count() == 0
                # Only the globally unique account alias created by this run.
                owned = database.connection.execute(
                    select(chunks.c.message_id).where(
                        chunks.c.account == account,
                        chunks.c.chat_id == chat,
                    )
                ).scalars().all()
                if owned:
                    asyncio.run(client.delete_messages(
                        resolved, sorted(set(int(mid) for mid in owned)),
                    ))
            finally:
                asyncio.run(client.disconnect())
        finally:
            for proc, _ in processes:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=10)
            database.close()


async def _collect_range(engine, ident, size, start):
    return b"".join([
        part async for part in engine.iter_bytes(
            ident, start=start, end=min(size, start+37),
            block_size=7,
        )
    ])


def test_embedded_worker_script_is_valid_python():
    """The subprocess worker must compile before any live CI run."""
    compile(WORKER, "<multiworker-e2e-worker>", "exec")


def test_worker_script_processes_jobs_with_fake_telethon(tmp_path):
    """End-to-end worker flow (SQL jobs + fake MTProto) without live Telegram.

    Spawns the embedded worker subprocess with a stubbed telethon package so
    the claim/connect/upload/journal loop is verified before any live CI run.
    """
    import textwrap
    from types import SimpleNamespace

    fake_telethon = tmp_path / "fake_telethon"
    package = fake_telethon / "telethon"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from .client import TelegramClient\n", encoding="utf-8"
    )
    (package / "sessions.py").write_text(
        "StringSession = lambda value: value\n", encoding="utf-8"
    )
    (package / "errors.py").write_text(
        "class FloodWaitError(Exception):\n"
        "    def __init__(self, request=None, capture=0):\n"
        "        self.seconds = int(capture)\n"
        "        super().__init__('flood wait %d' % self.seconds)\n",
        encoding="utf-8",
    )
    (package / "client.py").write_text(textwrap.dedent("""
        from types import SimpleNamespace

        class TelegramClient:
            def __init__(self, session, api_id, api_hash, **kwargs):
                self.session = session
                self.posts = {}
                self.next_id = 1000

            async def connect(self):
                pass

            async def disconnect(self):
                pass

            def is_connected(self):
                return True

            async def get_peer_id(self, target):
                return -100123456

            async def iter_dialogs(self, limit=None):
                entity = SimpleNamespace(username=None)
                dialog = SimpleNamespace(
                    id=-100123, is_channel=True, is_group=False,
                    entity=entity, name="Test Channel",
                )
                if limit:
                    assert limit > 0
                    yield dialog
                else:
                    yield dialog

            async def get_entity(self, target):
                return SimpleNamespace(username=None)

            async def send_file(self, target, path, caption=None,
                                force_document=False):
                from pathlib import Path
                assert caption.startswith("teledrive:")
                self.next_id += 1
                self.posts[self.next_id] = Path(path).read_bytes()
                return SimpleNamespace(id=self.next_id, chat_id=-100123456)
        """), encoding="utf-8")

    db_url = "sqlite:///" + str(tmp_path / "state.db")
    from teledrive.db import connect_database
    from teledrive.storage.catalog import BucketCatalog
    from teledrive.storage.jobs import StorageUploadJobs
    from teledrive.storage.manifest import ManifestStore

    source = tmp_path / "sources"
    source.mkdir()
    body = b"worker-job-bytes" * 100
    payload = source / "job.bin"
    payload.write_bytes(body)
    db = connect_database(db_url)
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket("worker-ci", 1)
        StorageUploadJobs(catalog).enqueue(
            "worker-ci", "a.bin", payload, allowed_root=source,
        )
    finally:
        db.close()

    ready = tmp_path / "ready"
    go = tmp_path / "go"
    env = {
        "PYTHONPATH": str(fake_telethon),
        "TELEDRIVE_E2E_SESSION": "fake-session",
        "TELEDRIVE_E2E_API_ID": "12345",
        "TELEDRIVE_E2E_API_HASH": "a" * 32,
    }
    import os
    proc_env = dict(os.environ)
    proc_env.update(env)
    # The live workflow exports these; the fake-telethon run must resolve
    # by the numeric target argv instead of a real channel title.
    proc_env.pop("TELEDRIVE_E2E_CHANNEL_NAME", None)
    proc_env.pop("TELEDRIVE_E2E_TARGET", None)
    process = subprocess.Popen(
        [sys.executable, "-c", WORKER, db_url, str(source),
         str(ready), str(go), "-100123", "worker-account"],
        env=proc_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=Path(__file__).resolve().parents[2],
    )
    deadline = time.monotonic() + 30
    while not ready.exists():
        if time.monotonic() >= deadline or process.poll() is not None:
            process.kill()
            raise AssertionError("worker did not reach the ready gate")
        time.sleep(0.05)
    go.write_text("go", encoding="ascii")
    stdout, stderr = process.communicate(timeout=120)
    assert process.returncode == 0, stderr[-2000:]
    db = connect_database(db_url)
    try:
        catalog = BucketCatalog(ManifestStore(db))
        rows = StorageUploadJobs(catalog).list_jobs(limit=10)
        assert len(rows) == 1 and rows[0]["state"] == "done"
        item = catalog.get("worker-ci", "a.bin").item
        assert item.sha256 == hashlib.sha256(body).hexdigest()
    finally:
        db.close()
