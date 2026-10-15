#!/usr/bin/env python3
"""Staging DR: verify SQL snapshot, then read the original Telegram bytes.

No live database is modified and no Telegram uploads or deletes are issued.
The verified backup is copied into a disposable scratch directory before
initializing ORM metadata, preserving the exact SHA-256 receipt of the
operator's archive. The CLI authenticates a read-only USER MTProto session
against the ORIGINAL channel; no Bot token is required. Isolated historical
Bot transport fixtures remain available to prove backward compatibility.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from scripts.verify_metadata_backup import verify_snapshot
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.manifest import ManifestStore
from teledrive.storage.bot_api_adapter import BotApiChunkTransport, BotFileIndex
from teledrive.storage.telethon_adapter import TelethonChunkTransport

class ReadOnlyUserChannelTransport:
    """Permit only an exact authorized channel and original SQL account label.

    Historical api-bot messages in the *same* channel may be read by a real
    Telegram user. No mutation method is exposed; no cross-channel fallback.
    Chunk hash and full-object integrity remain checked by the storage engine.
    """

    def __init__(self, client, channel, *, account: str, chat_id: int):
        self.account = account
        self.chat_id = chat_id
        self.reader = TelethonChunkTransport({account: (client, channel)})

    async def download(self, reference, destination):
        if (reference.chat_id != self.chat_id or
                reference.account not in (self.account, "api-bot")):
            raise FileNotFoundError("Original account/channel scope does not match")
        return await self.reader.download(
            replace(reference, account=self.account), destination,
        )


async def verify_restored_telegram_object(
    snapshot: Path, receipt_sha256: str, bot=None, *,
    bucket: str, key: str, account: str, chat_id: int,
    max_bytes: int = 64 * 1024 * 1024,
    transport=None,
) -> dict:
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024**3:
        raise ValueError("Invalid staging DR maximum object size")
    if transport is not None and bot is not None:
        raise ValueError("Specify a single Telegram read transport")
    if transport is None and bot is None:
        raise ValueError("No authorized Telegram read transport configured")
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
            reader = transport
            if reader is None:
                # Historical integration tests for already-stored Bot media.
                # The operator-facing CLI NEVER selects this old write adapter.
                reader = BotApiChunkTransport(
                    bot, BotFileIndex(db), account=account, chat_id=chat_id,
                    min_upload_interval=0,
                )
            engine = TelegramObjectStorage(
                manifests, reader, scratch_directory=root,
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


async def verify_original_user_channel(
    snapshot: Path, receipt_sha256: str, *, bucket: str, key: str,
    account: str, chat_id: int, target: str,
    session_file: Path, api_id: int, api_hash: str,
    max_bytes: int = 64 * 1024 * 1024,
) -> dict:
    """Read owner-authorized original Telegram messages without mutations."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import Channel

    if api_id <= 0 or not api_hash or not target or not account:
        raise ValueError("Missing authorized user MTProto configuration")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(session_file, flags)
    with os.fdopen(descriptor, "r", encoding="ascii") as stream:
        session = stream.read(8193).strip()
    if not session or len(session) > 8192:
        raise ValueError("Invalid private user StringSession file")
    client = TelegramClient(
        StringSession(session), api_id, api_hash,
        device_model="TeleDrive Storage Client",
        app_version="TeleDrive 2.1",
        receive_updates=False, flood_sleep_threshold=0,
    )
    try:
        await client.connect()
        identity = await client.get_me()
        if identity is None or getattr(identity, "bot", False):
            raise ValueError("Staging original-media DR requires a USER session")
        if target.lstrip("-").isdigit():
            resolved = None
            async for dialog in client.iter_dialogs(limit=5000):
                if int(dialog.id) == int(target):
                    resolved = dialog.entity
                    break
            if resolved is None:
                raise FileNotFoundError("Original Telegram channel not in user dialogs")
        else:
            resolved = await client.get_entity(target)
        if not isinstance(resolved, Channel) or not getattr(resolved, "broadcast", False):
            raise ValueError("Staging original-media DR requires a broadcast channel")
        actual_chat_id = int(await client.get_peer_id(resolved))
        if actual_chat_id != chat_id:
            raise ValueError("Original Telegram channel identity mismatch")
        reader = ReadOnlyUserChannelTransport(
            client, resolved, account=account, chat_id=chat_id,
        )
        return await verify_restored_telegram_object(
            snapshot, receipt_sha256, bucket=bucket, key=key,
            account=account, chat_id=chat_id, max_bytes=max_bytes,
            transport=reader,
        )
    finally:
        await client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--sha256", required=True, help="SHA256 from online backup receipt")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--target", required=True, help="Exact authorized original channel")
    parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    try:
        api_id = int(os.environ["TELEDRIVE_STORAGE_MTPROTO_API_ID"])
        api_hash = os.environ["TELEDRIVE_STORAGE_MTPROTO_API_HASH"]
        session_file = Path(os.environ.get(
            "TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE", "config/mtproto-user.session",
        ))
        result = asyncio.run(verify_original_user_channel(
            args.snapshot, args.sha256, bucket=args.bucket, key=args.key,
            account=args.account, chat_id=args.chat_id, target=args.target,
            session_file=session_file, api_id=api_id, api_hash=api_hash,
            max_bytes=args.max_bytes,
        ))
    except Exception as exc:
        parser.exit(1, "User MTProto DR verification failed: " + type(exc).__name__ + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
