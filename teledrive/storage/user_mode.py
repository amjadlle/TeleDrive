"""Owner-authorized user MTProto transport for the single REST/S3 catalog.

Only an existing user StringSession is accepted. Channel targets are required:
Saved Messages, bot sessions and private chat destinations are intentionally
not storage targets. The session runs on the web application's ASGI event loop.
An acknowledged send is persisted before the object engine writes its chunk.
Pending sends are never retried automatically after an ambiguous failure.
"""
from __future__ import annotations

from dataclasses import replace
import os
import platform
import re
from pathlib import Path

from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, insert, update
from sqlalchemy.exc import IntegrityError

from .telethon_adapter import TelethonChunkTransport
from .bot_api_adapter import BotApiChunkTransport

_meta = MetaData()
mtproto_send_intents = Table(
    "storage_mtproto_send_intents", _meta,
    Column("account", String(255), primary_key=True),
    Column("chat_id", BigInteger, primary_key=True),
    Column("object_id", String(36), primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("sha256", String(64), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("message_id", BigInteger),
)


def transport_mode() -> str:
    value = os.environ.get("TELEDRIVE_STORAGE_TRANSPORT", "bot").strip().lower()
    if value not in ("bot", "telethon"):
        raise RuntimeError("TELEDRIVE_STORAGE_TRANSPORT must be bot or telethon")
    return value


def _session_from_file() -> str:
    filename = os.environ.get("TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE", "").strip()
    if not filename:
        raise RuntimeError("Set TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE for user storage")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(filename, flags)
    with os.fdopen(fd, "r", encoding="ascii") as handle:
        value = handle.read(8193).strip()
    if not value or len(value) > 8192:
        raise RuntimeError("Invalid authorized user session file")
    return value


class UserSessionRuntime:
    """A single user-authorized client owned by the ASGI lifespan."""

    def __init__(self):
        self.client = None
        self.transport = None
        self.account = None
        self.chat_id = None

    async def start(self) -> None:
        if transport_mode() != "telethon":
            return
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.tl.types import Channel

        if self.client is not None:
            raise RuntimeError("User storage client already started")
        api_id = int(os.environ["TELEDRIVE_STORAGE_MTPROTO_API_ID"])
        api_hash = os.environ["TELEDRIVE_STORAGE_MTPROTO_API_HASH"].strip()
        target_id = os.environ["TELEDRIVE_STORAGE_MTPROTO_CHANNEL"].strip()
        account = os.environ.get("TELEDRIVE_STORAGE_MTPROTO_ACCOUNT", "mtproto-user").strip()
        if (api_id <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash)
                or not target_id or target_id.lower() in ("me", "self", "saved messages")
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account)
                or account == "api-bot"):
            raise RuntimeError("Invalid user storage account or dedicated channel configuration")
        session = _session_from_file()
        # These are honest TeleDrive client details, not a forged official
        # Telegram Desktop/Android fingerprint. MTProto has no HTTP User-Agent.
        client = TelegramClient(
            StringSession(session), api_id, api_hash,
            device_model="TeleDrive Storage Client",
            system_version=platform.system(),
            app_version="TeleDrive 2.1",
            lang_code="en", system_lang_code="en",
            receive_updates=False, flood_sleep_threshold=0,
        )
        try:
            await client.connect()
            identity = await client.get_me()
            if identity is None or getattr(identity, "bot", False):
                raise RuntimeError("MTProto storage requires an authorized Telegram USER session")
            if target_id.lstrip("-").isdigit():
                # StringSession carries no entity cache. A private channel ID
                # must be resolved from the user's own dialogs, never guessed.
                wanted = int(target_id)
                channel = None
                async for dialog in client.iter_dialogs(limit=5000):
                    if int(dialog.id) == wanted:
                        channel = dialog.entity
                        break
                if channel is None:
                    raise RuntimeError("Configured channel not found in authorized user's dialogs")
            else:
                channel = await client.get_entity(target_id)
            if not isinstance(channel, Channel) or not getattr(channel, "broadcast", False):
                raise RuntimeError("MTProto storage target must be a channel, not Saved Messages")
            rights = getattr(channel, "admin_rights", None)
            if not (getattr(channel, "creator", False)
                    or (rights is not None and getattr(rights, "post_messages", False))):
                raise RuntimeError("User must have channel posting rights")
            chat_id = int(await client.get_peer_id(channel))
            self.account, self.chat_id = account, chat_id
            self.client = client
            self.transport = TelethonChunkTransport({account: (client, channel)})
        except BaseException:
            await client.disconnect()
            raise

    async def stop(self) -> None:
        client = self.client
        self.client = self.transport = self.account = self.chat_id = None
        if client is not None:
            await client.disconnect()

    def require(self):
        if self.client is None or self.transport is None or not self.client.is_connected():
            raise RuntimeError("User Telegram session is not connected")
        return self.transport


class UserSendJournal:
    def __init__(self, db):
        self.db = db
        _meta.create_all(db.engine)

    def start(self, account: str, chat_id: int, object_id: str, ordinal: int,
              sha256: str, size: int) -> None:
        if (not account or not chat_id or ordinal < 0 or size < 1
                or size > 1024 * 1024 * 1024
                or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
            raise ValueError("Invalid MTProto upload intent")
        try:
            self.db.connection.execute(insert(mtproto_send_intents).values(
                account=account, chat_id=chat_id, object_id=object_id,
                ordinal=ordinal, sha256=sha256, size=size, message_id=None,
            ))
            self.db.commit()
        except IntegrityError:
            self.db.connection.rollback()
            raise RuntimeError("Unresolved MTProto upload intent: reconcile before retry") from None

    def acknowledged(self, account: str, chat_id: int, object_id: str,
                     ordinal: int, message_id: int) -> None:
        changed = self.db.connection.execute(update(mtproto_send_intents).where(
            mtproto_send_intents.c.account == account,
            mtproto_send_intents.c.chat_id == chat_id,
            mtproto_send_intents.c.object_id == object_id,
            mtproto_send_intents.c.ordinal == ordinal,
            mtproto_send_intents.c.message_id.is_(None),
        ).values(message_id=message_id))
        if changed.rowcount != 1:
            self.db.connection.rollback()
            raise RuntimeError("MTProto upload receipt could not be journaled")
        self.db.commit()


class UnifiedUserTransport:
    """Write new chunks by user MTProto; read old Bot chunks when configured."""

    def __init__(self, runtime: UserSessionRuntime, journal: UserSendJournal,
                 bot_fallback: BotApiChunkTransport | None = None):
        self.runtime = runtime
        self.journal = journal
        self.bot_fallback = bot_fallback

    async def verify_target(self):
        self.runtime.require()

    async def upload(self, path: Path, *, object_id: str, ordinal: int,
                     checksum: str):
        delegate = self.runtime.require()
        account, chat_id = self.runtime.account, self.runtime.chat_id
        self.journal.start(account, chat_id, object_id, ordinal,
                           checksum, path.stat().st_size)
        # Uncertain send errors leave a durable intent for history recovery.
        # Normalize remote I/O errors to an opaque backend failure for REST/S3
        # without leaking Telegram message text, session data or phone details.
        try:
            received_account, received_chat, message_id = await delegate.upload(
                path, object_id=object_id, ordinal=ordinal, checksum=checksum,
            )
            if (received_account, received_chat) != (account, chat_id):
                raise RuntimeError("MTProto send returned an unexpected account or channel")
            self.journal.acknowledged(account, chat_id, object_id, ordinal, message_id)
            return received_account, received_chat, message_id
        except Exception:
            raise RuntimeError(
                "MTProto send outcome uncertain; reconcile protected intent before retry"
            ) from None

    async def download(self, reference, destination):
        if reference.account == self.runtime.account:
            return await self.runtime.require().download(reference, destination)
        if reference.account == "api-bot":
            # Historic Bot API objects already persist their real Telegram
            # channel/message coordinates in the immutable SQL manifest.
            # An authorized USER of that *same channel* can independently
            # read those messages, without the Bot token or old file_id.
            # Never attempt another account's channel or mutate SQL refs.
            if reference.chat_id == self.runtime.chat_id:
                try:
                    rebound = replace(reference, account=self.runtime.account)
                    return await self.runtime.require().download(rebound, destination)
                except FileNotFoundError:
                    if self.bot_fallback is None:
                        raise
            if self.bot_fallback is not None:
                return await self.bot_fallback.download(reference, destination)
        raise FileNotFoundError("Storage source unavailable: original transport required")
