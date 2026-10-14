#!/usr/bin/env python3
"""Offline long-duration SQL/Telegram-object-engine capacity probe.

Uses real SQLite SQL transactions + the real streaming object engine, with
in-memory deterministic Telegram transport. This is NOT live Telegram or
physical multi-host acceptance. No user credentials, media deletion or ports.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import tempfile
import time
from pathlib import Path

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.manifest import ManifestStore


class IsolatedMedia:
    def __init__(self):
        self.data = {}
        self.next_id = 0

    async def upload(self, path, *, object_id, ordinal, checksum):
        payload = Path(path).read_bytes()
        if hashlib.sha256(payload).hexdigest() != checksum:
            raise ValueError("Uploader checksum mismatch")
        self.next_id += 1
        ref = ("synthetic", 123456, self.next_id)
        self.data[ref] = payload
        return ref

    async def download(self, reference, destination):
        key = (reference.account, reference.chat_id, reference.message_id)
        if key not in self.data:
            raise FileNotFoundError("Synthetic remote message missing")
        Path(destination).write_bytes(self.data[key])


def percentile(values: list[float], fraction: float) -> float:
    assert values and 0 <= fraction <= 1
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)], 3)


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
    workspace.mkdir(parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise FileExistsError("Capacity workspace must be empty")
    dbpath = workspace / "isolated-sqlite.db"
    backend = IsolatedMedia()
    samples = []
    count = 0
    start = clock()
    db = connect_database(str(dbpath))
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket("capacity-test", int(time.time()))
        while True:
            source = workspace / "original.bin"
            recovered = workspace / "recovered.bin"
            data = os.urandom(object_bytes)
            source.write_bytes(data)
            begin = clock()
            obj = asyncio.run(TelegramObjectStorage(
                catalog.manifests, backend, chunk_size=128 * 1024,
                scratch_directory=workspace,
            ).put_file("capacity-test", f"items/{count:08d}", source))
            catalog.publish(obj.id, now=int(time.time()))
            source.unlink()
            asyncio.run(TelegramObjectStorage(
                catalog.manifests, backend, chunk_size=128 * 1024,
                scratch_directory=workspace,
            ).download_to_path(obj.id, recovered))
            if recovered.read_bytes() != data or obj.sha256 != hashlib.sha256(data).hexdigest():
                raise ValueError("Byte-for-byte Telegram engine recovery failed")
            recovered.unlink()
            # Retain immutable SQL rows as an increasing catalog workload,
            # but do not keep remote payload copies unbounded in memory.
            backend.data.clear()
            samples.append(max(0.0, (clock() - begin) * 1000))
            count += 1
            if clock() - start >= duration_seconds:
                break
            if interval_seconds:
                sleep(interval_seconds)
    finally:
        db.close()
    elapsed = clock() - start
    return {
        "mode": "isolated-sqlite-deterministic-telegram",
        "duration_seconds": round(elapsed, 2),
        "object_bytes": object_bytes,
        "verified_roundtrips": count,
        "verified_bytes": object_bytes * count,
        "p50_roundtrip_ms": percentile(samples, .5),
        "p95_roundtrip_ms": percentile(samples, .95),
        "max_roundtrip_ms": round(max(samples), 3),
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
