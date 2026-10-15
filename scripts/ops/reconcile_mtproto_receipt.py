#!/usr/bin/env python3
"""Operator-gated USER-MTProto receipt reconciliation with channel proof.

A lost send intent is resolved ONLY by the authorized USER session reading
the exact original channel message: caption marker identity, full SHA-256
and size are verified before any SQL receipt is recorded. Nothing is
deleted or resent; --apply records a receipt only after all checks pass.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import tempfile
from pathlib import Path


async def run(args) -> dict:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from teledrive.db import connect_database
    from teledrive.storage.user_mode import UserSendJournal

    api_id = int(os.environ["TELEDRIVE_RECONCILE_API_ID"])
    api_hash = os.environ["TELEDRIVE_RECONCILE_API_HASH"]
    session = os.environ["TELEDRIVE_RECONCILE_SESSION"].strip()
    if not api_id or not api_hash or not session:
        raise ValueError("Missing reconciliation MTProto configuration")
    if (not args.account or not args.object_id or args.ordinal < 0
            or args.message_id <= 0
            or not re_full_sha(args.sha256)):
        raise ValueError("Invalid reconciliation identity")

    client = TelegramClient(
        StringSession(session), api_id, api_hash,
        device_model="TeleDrive Storage Client",
        app_version="TeleDrive 2.1",
        receive_updates=False, flood_sleep_threshold=0,
    )
    await client.connect()
    try:
        target = args.target.strip()
        if target.lstrip("-").isdigit():
            entity = None
            async for dialog in client.iter_dialogs(limit=5000):
                if int(dialog.id) == int(target):
                    entity = dialog.entity
                    break
            if entity is None:
                raise FileNotFoundError("Original channel not in user dialogs")
        else:
            entity = await client.get_entity(target)
        if int(await client.get_peer_id(entity)) != args.chat_id:
            raise ValueError("Original Telegram channel identity mismatch")
        message = await client.get_messages(entity, ids=args.message_id)
        if message is None or not getattr(message, "media", None):
            raise ValueError("Original Telegram message/media is missing")
        expected_caption = (
            f"teledrive:{args.object_id}:{args.ordinal}:{args.sha256[:16]}"
        )
        if getattr(message, "message", "") != expected_caption:
            raise ValueError("Original message caption does not match the intent")
        with tempfile.TemporaryDirectory(prefix="teledrive-reconcile-") as name:
            destination = Path(name) / "original.bin"
            result = await client.download_media(message, file=str(destination))
            if result is None:
                raise ValueError("Original Telegram media download failed")
            digest = hashlib.sha256()
            size = 0
            with destination.open("rb") as media:
                while block := media.read(1024 * 1024):
                    digest.update(block)
                    size += len(block)
            if digest.hexdigest() != args.sha256 or size != args.size:
                raise ValueError("Original Telegram bytes do not match the intent")
        applied = False
        if args.apply:
            db = connect_database(args.database)
            try:
                UserSendJournal(db).acknowledged(
                    args.account, args.chat_id, args.object_id, args.ordinal,
                    args.message_id,
                )
                applied = True
            finally:
                db.close()
        return {
            "verified": True,
            "applied": applied,
            "message_id": args.message_id,
            "sha256": args.sha256,
            "size": args.size,
            "telegram_uploads": 0,
            "telegram_deletions": 0,
        }
    finally:
        await client.disconnect()


def re_full_sha(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--target", required=True,
                        help="Exact authorized original channel")
    parser.add_argument("--object-id", required=True)
    parser.add_argument("--ordinal", type=int, required=True)
    parser.add_argument("--message-id", type=int, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--apply", action="store_true",
                        help="Record the verified receipt into the SQL journal")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        parser.exit(1, "MTProto receipt verification failed: "
                       + type(exc).__name__ + "\n")
    import json
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
