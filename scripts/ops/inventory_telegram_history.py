#!/usr/bin/env python3
"""Inventory original Telegram marker candidates without touching stored media.

Supply TELEDRIVE_REBUILD_API_ID, TELEDRIVE_REBUILD_API_HASH and
TELEDRIVE_REBUILD_SESSION from a protected operator environment.
The 0600 JSON report includes original Telegram message coordinates and must
never be committed, uploaded to CI, or treated as a restoration manifest.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from teledrive.storage.history_inventory import (
    inventory_channel, write_private_report,
)


async def execute(*, account: str, target: str, limit: int,
                  api_id: int, api_hash: str, session: str) -> dict:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        entity = await client.get_entity(target)
        return await inventory_channel(
            client, entity, account=account, scan_limit=limit,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="Original user-account label")
    parser.add_argument("--target", required=True, help="Authorized original channel")
    parser.add_argument("--limit", type=int, default=1000,
                        help="Maximum messages inspected (1..10000)")
    parser.add_argument("--output", required=True, type=Path,
                        help="New private JSON path; existing files are never replaced")
    args = parser.parse_args()
    try:
        # Validate arguments and report destination before authenticating.
        if not 1 <= args.limit <= 10_000:
            raise ValueError("Invalid scan limit")
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("Refuse to replace an existing inventory")
        if not args.output.parent.is_dir():
            raise FileNotFoundError("Inventory output directory missing")
        api_id = int(os.environ["TELEDRIVE_REBUILD_API_ID"])
        api_hash = os.environ["TELEDRIVE_REBUILD_API_HASH"]
        session = os.environ["TELEDRIVE_REBUILD_SESSION"]
        if not api_hash or not session:
            raise ValueError("Missing original Telegram credentials")
        report = asyncio.run(execute(
            account=args.account, target=args.target, limit=args.limit,
            api_id=api_id, api_hash=api_hash, session=session,
        ))
        write_private_report(args.output, report)
    except Exception as exc:
        parser.exit(1, "Read-only Telegram inventory refused: "
                    + type(exc).__name__ + "\n")

    # Avoid leaking channel IDs and individual Telegram message IDs to logs.
    print(json.dumps({
        "report_created": True,
        "scanned_messages": report["scanned_messages"],
        "truncated": report["truncated"],
        "candidate_object_count": report["candidate_object_count"],
        "candidate_chunk_count": report["candidate_chunk_count"],
        "payload_verified": False,
        "production_database_modified": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
