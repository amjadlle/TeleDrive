#!/usr/bin/env python3
"""Staging DR: verify SQL snapshot, then read the original Telegram bytes.

No live database is modified and no Telegram uploads or deletes are issued.
The verified backup is copied into a disposable scratch directory before
initializing ORM metadata, preserving the exact SHA-256 receipt of the
operator's archive. Real bot credentials must be provided via environment.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from scripts.verify_metadata_backup import verify_snapshot
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.manifest import ManifestStore
from teledrive.storage.bot_api_adapter import BotApiChunkTransport, BotFileIndex, BotApiClient


async def verify_restored_telegram_object(
    snapshot: Path, receipt_sha256: str, bot, *,
    bucket: str, key: str, account: str, chat_id: int,
    max_bytes: int = 64 * 1024 * 1024,
) -> dict:
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024**3:
        raise ValueError("Invalid staging DR maximum object size")
    original_report = verify_snapshot(Path(snapshot), receipt_sha256)
    with tempfile.TemporaryDirectory(prefix="teledrive-staging-dr-") as dirname:
        root = Path(dirname)
        clone = root / "restored-metadata.db"
        shutil.copyfile(snapshot, clone)
        # Verify the clone's identical bytes before any ORM may add tables.
        if hashlib.sha256(clone.read_bytes()).hexdigest() != receipt_sha256.lower():
            raise ValueError("Staging clone differs from authenticated SQL snapshot")
        db = connect_database(str(clone))
        try:
            manifests = ManifestStore(db)
            catalog = BucketCatalog(manifests)
            record = catalog.get(bucket, key)
            obj = record.item
            if obj.size > max_bytes:
                raise ValueError("Staging DR sample exceeds explicit size budget")
            transport = BotApiChunkTransport(
                bot, BotFileIndex(db), account=account, chat_id=chat_id,
                min_upload_interval=0,
            )
            engine = TelegramObjectStorage(
                manifests, transport, scratch_directory=root,
            )
            target = root / "verified-telegram-payload"
            await engine.download_to_path(obj.id, target)
            with target.open("rb") as media:
                digest = hashlib.sha256()
                read_size = 0
                while block := media.read(1024 * 1024):
                    digest.update(block)
                    read_size += len(block)
            if digest.hexdigest() != obj.sha256 or read_size != obj.size:
                raise ValueError("Staging DR Telegram payload checksum mismatch")
            # Independently cross-check a half-open byte window across a
            # retrieved chunk boundary, not only the whole file SHA.
            start = max(0, obj.size // 2 - 17)
            end = min(obj.size, start + 37)
            observed = b"".join([
                piece async for piece in engine.iter_bytes(
                    obj.id, start=start, end=end, block_size=7,
                )
            ])
            with target.open("rb") as file:
                file.seek(start)
                expected = file.read(end - start)
            if observed != expected:
                raise ValueError("Staging DR Telegram Range read differs from verified file")
            return {
                "verified": True, "archive_sha256": original_report["sha256"],
                "object_id": obj.id, "size": obj.size,
                "sha256": obj.sha256, "sample_range": [start, end],
                "readback_bytes": read_size, "telegram_uploads": 0,
                "telegram_deletions": 0, "source_database_modified": False,
            }
        finally:
            db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--sha256", required=True, help="SHA256 from online backup receipt")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    token = os.getenv("TELEDRIVE_BOT_TOKEN")
    if not token:
        parser.exit(1, "Missing TELEDRIVE_BOT_TOKEN environment credential\n")
    try:
        result = asyncio.run(verify_restored_telegram_object(
            args.snapshot, args.sha256, BotApiClient(token),
            bucket=args.bucket, key=args.key, account=args.account,
            chat_id=args.chat_id, max_bytes=args.max_bytes,
        ))
    except Exception as exc:
        parser.exit(1, "DR verification failed: " + type(exc).__name__ + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
