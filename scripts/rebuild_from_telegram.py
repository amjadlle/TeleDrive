#!/usr/bin/env python3
"""Recover original Telegram bytes into a NEW isolated SQLite catalog.

Requires an independently verified recovery plan with bucket/key, object UUID,
size, full file SHA-256, all Telegram chunk hashes and message coordinates.
Original authorized Telegram user session is required; never touches the
existing production database, sends messages or deletes Telegram media.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from teledrive.storage.rebuild import restore_isolated_catalog, validate_plan
from teledrive.storage.telethon_adapter import TelethonChunkTransport


async def execute(plan: dict, destination: Path, *, account: str, target: str,
                  api_id: int, api_hash: str, session: str) -> dict:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    refs, _ = validate_plan(plan)
    if not refs:
        raise ValueError("Live source ownership requires at least one Telegram message")
    if any(ref.account != account for ref in refs):
        raise ValueError("One rebuild must use the same independently authorized account")
    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        entity = await client.get_entity(target)
        original_chat = int(await client.get_peer_id(entity))
        if any(ref.chat_id != original_chat for ref in refs):
            raise ValueError("Original Telegram channel does not match the recovery plan")
        remote = TelethonChunkTransport({account: (client, entity)})
        return await restore_isolated_catalog(plan, remote, destination)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--new-database", type=Path, required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--target", required=True, help="Original Telegram target")
    args = p.parse_args()
    try:
        api_id = int(os.environ["TELEDRIVE_REBUILD_API_ID"])
        api_hash = os.environ["TELEDRIVE_REBUILD_API_HASH"]
        session = os.environ["TELEDRIVE_REBUILD_SESSION"]
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if not api_hash or not session:
            raise ValueError("Missing original Telegram account credentials")
        result = asyncio.run(execute(
            plan, args.new_database, account=args.account,
            target=args.target, api_id=api_id, api_hash=api_hash,
            session=session,
        ))
    except Exception as exc:
        p.exit(1, "Original Telegram reconstruction refused: "
               + type(exc).__name__ + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
