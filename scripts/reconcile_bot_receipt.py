#!/usr/bin/env python3
"""Reconcile a lost Bot upload ACK using independent Telegram user evidence.

Defaults to DRY-RUN. Requires original exact SQL intent, Bot getFile bytes,
and separately fetched channel-post bytes with matching SHA-256 and size.
Run only as an authorized operator, on a verified database backup/staging
first. No Telegram uploads or deletion.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotApiClient, BotFileIndex
from teledrive.storage.receipt_reconcile import reconcile_bot_receipt


async def run(args) -> dict:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    bot_token = os.environ.get("TELEDRIVE_BOT_TOKEN", "")
    api_id = os.environ.get("TELEDRIVE_E2E_API_ID", "")
    api_hash = os.environ.get("TELEDRIVE_E2E_API_HASH", "")
    session = os.environ.get("TELEDRIVE_E2E_SESSION", "")
    if not (bot_token and api_id and api_hash and session):
        raise ValueError("Missing Bot/user Telegram environment credentials")
    db = connect_database(args.database)
    client = TelegramClient(StringSession(session), int(api_id), api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Telegram user session is not authorized")
        return await reconcile_bot_receipt(
            BotFileIndex(db), BotApiClient(bot_token), client,
            account=args.account, chat_id=args.chat_id,
            object_id=args.object_id, ordinal=args.ordinal,
            message_id=args.message_id, file_id=args.file_id,
            apply=args.apply,
        )
    finally:
        await client.disconnect()
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True,
                        help="Operator-selected SQL connection URL or SQLite path")
    parser.add_argument("--account", required=True, help="Immutable Bot account alias")
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--object-id", required=True)
    parser.add_argument("--ordinal", type=int, required=True)
    parser.add_argument("--message-id", type=int, required=True)
    parser.add_argument("--file-id", required=True,
                        help="File ID from independently retained original Bot evidence")
    parser.add_argument("--apply", action="store_true",
                        help="Commit verified receipt; default only reads Telegram/SQL")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        # Never print third-party HTTP errors containing session/token/URL.
        parser.exit(1, "Reconciliation refused: " + type(exc).__name__ + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
