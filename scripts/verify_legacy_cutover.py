#!/usr/bin/env python3
"""Verify every eligible legacy source against finished SQL jobs and Telegram.

This command neither queues nor uploads. It is for an operator who has
already disabled the old uploader, quiesced the source tree and enabled
an approved durable cutover. Exits nonzero if ANY eligible file is missing,
unpublished, changed or not independently readable from Telegram.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotApiClient, BotApiChunkTransport, BotFileIndex
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.cutover_acceptance import verify_legacy_cutover
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore


async def run(args) -> dict:
    token = os.environ.get("TELEDRIVE_BOT_TOKEN")
    if not token:
        raise ValueError("Missing Bot API credential in environment")
    db = connect_database(args.database)
    try:
        cfg = SimpleNamespace(
            source_dir=str(args.source_root),
            allowed_extensions=[s.lower() for s in args.extensions.split(",") if s],
            max_file_size_mb=args.max_file_size_mb,
        )
        index = BotFileIndex(db)
        transport = BotApiChunkTransport(
            BotApiClient(token), index,
            account=args.account, chat_id=args.chat_id,
        )
        queue = StorageUploadJobs(BucketCatalog(ManifestStore(db)))
        return await verify_legacy_cutover(
            cfg, queue, transport, bucket=args.bucket, prefix=args.prefix,
            approved_root=args.allowed_root, max_files=args.max_files,
            max_bytes=args.max_bytes, scratch_directory=args.scratch_dir,
        )
    finally:
        db.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", required=True)
    p.add_argument("--source-root", type=Path, required=True)
    p.add_argument("--allowed-root", type=Path, required=True)
    p.add_argument("--scratch-dir", type=Path, required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--prefix", default="")
    p.add_argument("--account", required=True)
    p.add_argument("--chat-id", type=int, required=True)
    p.add_argument("--extensions", default="")
    p.add_argument("--max-file-size-mb", type=int, default=0)
    p.add_argument("--max-files", type=int, default=1000)
    p.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    args = p.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        # Do not leak local source paths, SQL credentials or Telegram URLs.
        p.exit(1, "Cutover verification refused: " + type(exc).__name__ + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
