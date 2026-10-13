"""Telegram Bot API transport for the shared Telegram-backed object engine.

Bot API cannot fetch arbitrary channel messages by message_id, so every bot
upload persists the returned document.file_id in a separate SQL index.
Payload bytes remain exclusively in Telegram; SQLite/other SQL stores IDs only.

Official cloud Bot API downloads are limited to 20 MB per file. Keep chunk
sizes below that limit (16 MiB recommended), even though uploads allow 50 MB.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from sqlalchemy import BigInteger, Column, MetaData, String, Table, insert, select, delete

from .manifest import ChunkReference

BOT_DOWNLOAD_LIMIT = 20 * 1024 * 1024
BOT_RECOMMENDED_CHUNK_SIZE = 16 * 1024 * 1024

_bot_metadata = MetaData()
_bot_files = Table(
    "storage_bot_file_ids", _bot_metadata,
    Column("account", String(255), primary_key=True),
    Column("chat_id", BigInteger, primary_key=True),
    Column("message_id", BigInteger, primary_key=True),
    Column("file_id", String(1024), nullable=False),
)


class BotApiError(RuntimeError):
    """Safe error: never includes Bot API token, URL or Telegram response text."""


class BotFileIndex:
    """Durable Bot API file_id lookup shared across object engine restarts."""

    def __init__(self, database: Any):
        self.db = database
        _bot_metadata.create_all(database.engine)

    def put(self, account: str, chat_id: int, message_id: int, file_id: str) -> None:
        self.db.connection.execute(insert(_bot_files).values(
            account=account, chat_id=chat_id, message_id=message_id, file_id=file_id
        ))
        self.db.commit()

    def get(self, account: str, chat_id: int, message_id: int) -> str:
        result = self.db.connection.execute(select(_bot_files.c.file_id).where(
            _bot_files.c.account == account,
            _bot_files.c.chat_id == chat_id,
            _bot_files.c.message_id == message_id,
        )).scalar_one_or_none()
        if result is None:
            raise FileNotFoundError("Bot API file ID missing for this Telegram message")
        return result

    def forget(self, account: str, chat_id: int, message_id: int) -> None:
        self.db.connection.execute(delete(_bot_files).where(
            _bot_files.c.account == account,
            _bot_files.c.chat_id == chat_id,
            _bot_files.c.message_id == message_id,
        ))
        self.db.commit()


class BotApiClient:
    """Small stdlib HTTPS gateway. Token never enters exceptions or logs."""

    def __init__(self, token: str):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Invalid Telegram bot token format")
        self.__token = token

    @staticmethod
    def _multipart(fields: dict[str, Any], source: Path) -> tuple[bytes, str]:
        boundary = "teledrive-" + secrets.token_hex(12)
        output = bytearray()
        for key, value in fields.items():
            output.extend((
                f"--{boundary}\\r\\nContent-Disposition: form-data; name=\\"{key}\\"\\r\\n\\r\\n"
                f"{value}\\r\\n"
            ).encode("utf-8"))
        output.extend((
            f"--{boundary}\\r\\nContent-Disposition: form-data; "
            'name="document"; filename="chunk.bin"\\r\\n'
            "Content-Type: application/octet-stream\\r\\n\\r\\n"
        ).encode("ascii"))
        output.extend(source.read_bytes())
        output.extend(f"\\r\\n--{boundary}--\\r\\n".encode("ascii"))
        return bytes(output), f"multipart/form-data; boundary={boundary}"

    def _call_sync(self, method: str, fields: dict[str, Any],
                   source: Path | None = None) -> dict[str, Any]:
        if source is not None:
            body, content_type = self._multipart(fields, source)
        else:
            body = json.dumps(fields).encode("utf-8")
            content_type = "application/json"
        url = f"https://api.telegram.org/bot{self.__token}/{method}"
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": content_type}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise BotApiError(f"Bot API {method} response exceeded size limit")
            parsed = json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError, ValueError) as exc:
            raise BotApiError(f"Bot API {method} request failed") from None
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            code = parsed.get("error_code", "unknown") if isinstance(parsed, dict) else "unknown"
            raise BotApiError(f"Bot API {method} rejected request (code {code})")
        result = parsed.get("result")
        if result is True:  # deleteMessage and other success-only methods
            return {}
        if not isinstance(result, dict):
            raise BotApiError(f"Bot API {method} returned an invalid result")
        return result

    async def call(self, method: str, fields: dict[str, Any],
                   source: Path | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._call_sync, method, fields, source)

    @staticmethod
    def _safe_file_path(path: str) -> str:
        if (not isinstance(path, str) or not path or path.startswith("/")
                or "\\" in path or any(segment in ("", ".", "..") for segment in path.split("/"))
                or "://" in path):
            raise BotApiError("Bot API supplied an unsafe file path")
        return urllib.parse.quote(path, safe="/")

    def _download_sync(self, file_path: str, destination: Path) -> None:
        safe_path = self._safe_file_path(file_path)
        url = f"https://api.telegram.org/file/bot{self.__token}/{safe_path}"
        try:
            request = urllib.request.Request(url)
            with urllib.request.urlopen(request, timeout=90) as response:
                with destination.open("xb") as output:
                    size = 0
                    while data := response.read(1024 * 1024):
                        size += len(data)
                        if size > BOT_DOWNLOAD_LIMIT:
                            raise BotApiError("Telegram bot download exceeds 20 MiB")
                        output.write(data)
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise BotApiError("Bot API file download failed") from None
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    async def download_file(self, file_path: str, destination: Path) -> None:
        await asyncio.to_thread(self._download_sync, file_path, destination)


class BotApiChunkTransport:
    """TelegramTransport compatible adapter for a single bot/channel pairing."""

    def __init__(self, api: Any, index: BotFileIndex, *,
                 chat_id: int, account: str = "bot"):
        if not account or not isinstance(chat_id, int) or chat_id == 0:
            raise ValueError("A bot account alias and numeric channel ID are required")
        self.api, self.index, self.chat_id, self.account = api, index, chat_id, account

    async def verify_target(self) -> None:
        bot = await self.api.call("getMe", {})
        chat = await self.api.call("getChat", {"chat_id": self.chat_id})
        if chat.get("type") != "channel" or int(chat.get("id", 0)) != self.chat_id:
            raise BotApiError("Configured bot target must be a Telegram channel")
        member = await self.api.call("getChatMember", {
            "chat_id": self.chat_id, "user_id": int(bot["id"]),
        })
        if member.get("status") != "administrator" or not member.get("can_post_messages", False):
            raise BotApiError("Bot must be a channel administrator with post permission")

    async def upload(self, path: Path, *, object_id: str, ordinal: int,
                     checksum: str) -> tuple[str, int, int]:
        if path.stat().st_size > BOT_DOWNLOAD_LIMIT:
            raise ValueError("Bot API chunk exceeds cloud download limit (20 MiB)")
        message = await self.api.call("sendDocument", {
            "chat_id": self.chat_id,
            "caption": f"teledrive:{object_id}:{ordinal}:{checksum[:16]}",
        }, source=path)
        try:
            mid = int(message["message_id"])
            chat = int(message["chat"]["id"])
            fid = str(message["document"]["file_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BotApiError("Bot API did not return a document ID") from None
        if chat != self.chat_id or mid <= 0 or not fid:
            raise BotApiError("Bot API returned an unexpected upload target or file ID")
        self.index.put(self.account, chat, mid, fid)
        return self.account, chat, mid

    async def download(self, reference: ChunkReference, destination: Path) -> None:
        if reference.account != self.account or reference.chat_id != self.chat_id:
            raise FileNotFoundError("This bot does not own the requested chunk")
        fid = self.index.get(reference.account, reference.chat_id, reference.message_id)
        document = await self.api.call("getFile", {"file_id": fid})
        file_size = document.get("file_size")
        if file_size is not None and int(file_size) > BOT_DOWNLOAD_LIMIT:
            raise BotApiError("Bot API file exceeds download limit")
        path = document.get("file_path")
        if not isinstance(path, str):
            raise BotApiError("Bot API file has no download path")
        await self.api.download_file(path, destination)

    async def delete(self, reference: ChunkReference) -> None:
        if reference.account != self.account or reference.chat_id != self.chat_id:
            raise FileNotFoundError("Bot cannot delete a different target's message")
        # Only delete messages with an ID in our durable upload index.
        self.index.get(reference.account, reference.chat_id, reference.message_id)
        await self.api.call("deleteMessage", {
            "chat_id": reference.chat_id, "message_id": reference.message_id,
        })
        self.index.forget(reference.account, reference.chat_id, reference.message_id)
