#!/usr/bin/env python3
"""Isolated long-duration SQLite + persistent synthetic Telegram endurance.

Real storage engine and SQL transactions, disk-backed immutable remote chunks,
periodic SQL restarts and retrospective byte-level readback. No live Telegram
credentials, no physical multi-host load and no remote message deletion.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from pathlib import Path

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.manifest import ManifestStore


class IsolatedMedia:
    """Persist simulated Telegram message bytes separately from SQL metadata."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700)
        self.next_id = 0
        self.download_count = 0

    def path(self, message_id: int) -> Path:
        return self.root / f"{message_id:012d}.blob"

    async def upload(self, path, *, object_id, ordinal, checksum):
        payload = Path(path).read_bytes()
        if hashlib.sha256(payload).hexdigest() != checksum:
            raise ValueError("Uploader checksum mismatch")
        self.next_id += 1
        with self.path(self.next_id).open("xb") as target:
            target.write(payload)
        return ("synthetic", 123456, self.next_id)

    async def download(self, reference, destination):
        if reference.account != "synthetic" or reference.chat_id != 123456:
            raise FileNotFoundError("Synthetic remote identity mismatch")
        source = self.path(reference.message_id)
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError("Synthetic remote message missing")
        self.download_count += 1
        shutil.copyfile(source, destination)


def percentile(values: list[float], fraction: float) -> float:
    if not values or not 0 <= fraction <= 1:
        raise ValueError("Invalid percentile input")
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)], 3)


def _readback(catalog: BucketCatalog, media: IsolatedMedia, object_id: str,
              expected_sha: str, size: int, destination: Path,
              scratch: Path) -> None:
    engine = TelegramObjectStorage(
        catalog.manifests, media, chunk_size=128 * 1024,
        scratch_directory=scratch,
    )
    asyncio.run(engine.download_to_path(object_id, destination))
    digest = hashlib.sha256()
    observed = 0
    with destination.open("rb") as reader:
        while block := reader.read(1024 * 1024):
            observed += len(block)
            digest.update(block)
    destination.unlink()
    if observed != size or digest.hexdigest() != expected_sha:
        raise ValueError("Historical Telegram readback failed integrity proof")


def run(*, duration_seconds: int, object_bytes: int, interval_seconds: float = 1.0,
        workspace: Path | None = None, clock=time.monotonic, sleep=time.sleep) -> dict:
    if (type(duration_seconds) is not int or not 1 <= duration_seconds <= 21600
            or type(object_bytes) is not int or not 1 <= object_bytes <= 4 * 1024 * 1024
            or type(interval_seconds) not in (int, float)
            or not 0 <= interval_seconds <= 60):
        raise ValueError("Invalid bounded endurance parameters")
    if workspace is None:
        with tempfile.TemporaryDirectory(prefix="teledrive-capacity-") as dirname:
            return run(duration_seconds=duration_seconds, object_bytes=object_bytes,
                       interval_seconds=interval_seconds, workspace=Path(dirname),
                       clock=clock, sleep=sleep)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise FileExistsError("Capacity workspace must be empty")

    dbpath = workspace / "isolated-sqlite.db"
    media = IsolatedMedia(workspace / "telegram")
    samples: list[float] = []
    history: list[tuple[str, str, int]] = []
    historic_rereads = 0
    restarts = 0
    start = clock()
    db = connect_database(str(dbpath))
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket("capacity-test", int(time.time()))
        while True:
            original = workspace / "original.bin"
            recovered = workspace / "recovered.bin"
            payload = os.urandom(object_bytes)
            digest = hashlib.sha256(payload).hexdigest()
            original.write_bytes(payload)
            begin = clock()
            item = asyncio.run(TelegramObjectStorage(
                catalog.manifests, media, chunk_size=128 * 1024,
                scratch_directory=workspace,
            ).put_file("capacity-test", f"items/{len(history):08d}", original))
            catalog.publish(item.id, now=int(time.time()))
            original.unlink()
            _readback(catalog, media, item.id, digest, object_bytes, recovered, workspace)
            history.append((item.id, digest, object_bytes))
            samples.append(max(0.0, (clock() - begin) * 1000))

            # Reopen the actual catalog periodically, verify old remote bytes
            # without source files, and ensure the synthetic transport retained
            # its original immutable messages rather than discarding them.
            if len(history) % 8 == 0:
                db.close()
                db = connect_database(str(dbpath))
                catalog = BucketCatalog(ManifestStore(db))
                restarts += 1
                index = len(history) // 2 - 1
                old_id, old_sha, old_size = history[index]
                _readback(catalog, media, old_id, old_sha, old_size,
                          recovered, workspace)
                historic_rereads += 1

            if clock() - start >= duration_seconds:
                break
            if interval_seconds:
                sleep(interval_seconds)
        # A final fresh-connection readback proves that SQL and Telegram
        # survived end-to-end retention for the full duration.
        db.close()
        db = connect_database(str(dbpath))
        catalog = BucketCatalog(ManifestStore(db))
        restarts += 1
        for index in {0, len(history) // 2, len(history) - 1}:
            ident, sha, length = history[index]
            _readback(catalog, media, ident, sha, length,
                      workspace / "recovered.bin", workspace)
            historic_rereads += 1
    finally:
        db.close()
    elapsed = clock() - start
    files = tuple((workspace / "telegram").glob("*.blob"))
    total_disk_bytes = sum(p.stat().st_size for p in files)
    if total_disk_bytes != object_bytes * len(history):
        raise ValueError("Retained synthetic Telegram payloads are incomplete")
    return {
        "mode": "isolated-sqlite-persistent-disk-telegram",
        "duration_seconds": round(elapsed, 2),
        "object_bytes": object_bytes,
        "verified_roundtrips": len(history),
        "verified_bytes": object_bytes * len(history),
        "p50_roundtrip_ms": percentile(samples, .5),
        "p95_roundtrip_ms": percentile(samples, .95),
        "max_roundtrip_ms": round(max(samples), 3),
        "sql_restarts": restarts,
        "historical_rereads": historic_rereads,
        "retained_telegram_chunks": len(files),
        "retained_telegram_bytes": total_disk_bytes,
        "real_telegram_tested": False,
        "physical_hosts_tested": False,
        "production_acceptance_signed_off": False,
        "telegram_deletions": 0,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration-seconds", type=int, default=3600)
    p.add_argument("--object-bytes", type=int, default=65536)
    p.add_argument("--interval-seconds", type=float, default=1.0)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        result = run(duration_seconds=args.duration_seconds,
                     object_bytes=args.object_bytes, interval_seconds=args.interval_seconds)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(result, out, sort_keys=True, indent=2)
            out.write("\n")
        print(json.dumps(result, sort_keys=True))
        return 0 if result["duration_seconds"] >= args.duration_seconds else 2
    except Exception as exc:
        p.exit(1, "Capacity soak refused: " + type(exc).__name__ + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
