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
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from sqlalchemy import BigInteger, Column, Integer, Index, MetaData, String, Table, and_, func, insert, select, delete

from .manifest import ChunkReference, chunks

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
# Successfully acknowledged messages are indexed *before* the engine
# journals a chunk. This closes the recoverable ACK->manifest SQL crash
# window when the upload receipt commit itself succeeded. It cannot recover
# a response lost before this SQL commit (Bot API has no message history).
_bot_receipts = Table(
    "storage_bot_upload_receipts", _bot_metadata,
    Column("account", String(255), primary_key=True),
    Column("chat_id", BigInteger, primary_key=True),
    Column("message_id", BigInteger, primary_key=True),
    Column("object_id", String(36), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size", BigInteger, nullable=False),
)
Index("ix_storage_bot_upload_receipts_object",
      _bot_receipts.c.object_id, _bot_receipts.c.ordinal)

# Persist the transfer intent BEFORE any Telegram send. A lost network ACK
# or a process crash after the remote accepts a document otherwise leaves
# zero evidence in SQL and would cause an unsafe duplicate retransmission.
# The intent is cleared atomically with the durable receipt, or after a
# *definite* rejection (Telegram 429). Unknown outcomes require an operator.
_bot_send_intents = Table(
    "storage_bot_send_intents", _bot_metadata,
    Column("account", String(255), primary_key=True),
    Column("chat_id", BigInteger, primary_key=True),
    Column("object_id", String(36), primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("sha256", String(64), nullable=False),
    Column("size", BigInteger, nullable=False),
)


class BotApiError(RuntimeError):
    """Safe error: never includes Bot API token, URL or Telegram response text."""


class BotApiRateLimited(BotApiError):
    """Telegram explicitly refused a request (no remote send was accepted).

    This is distinct from ambiguous socket timeout/read errors, which must
    never be blindly retried after a potentially accepted message.
    """

    def __init__(self, retry_after: int):
        if type(retry_after) is not int or not 1 <= retry_after <= 3600:
            raise ValueError("Invalid Bot API retry_after")
        self.retry_after = retry_after
        super().__init__("Telegram Bot API 429: retry after server cooldown")


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

    @staticmethod
    def _intent_predicate(account: str, chat_id: int, object_id: str, ordinal: int):
        return (
            _bot_send_intents.c.account == account,
            _bot_send_intents.c.chat_id == chat_id,
            _bot_send_intents.c.object_id == object_id,
            _bot_send_intents.c.ordinal == ordinal,
        )

    def start_send(self, account: str, chat_id: int, object_id: str,
                   ordinal: int, checksum: str, size: int) -> None:
        """Durably mark a send as potentially accepted before Telegram I/O.

        This is a per-object write fence, not an optional telemetry event.
        A stale intent must NEVER be silently ignored or automatically
        retransmitted, even when the previous worker crashed before sending.
        """
        if (not account or not chat_id or not object_id or len(object_id) > 36
                or ordinal < 0 or not 0 < size <= BOT_DOWNLOAD_LIMIT
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise ValueError("Invalid Bot API send intent")
        try:
            self.db.connection.execute(insert(_bot_send_intents).values(
                account=account, chat_id=chat_id, object_id=object_id,
                ordinal=ordinal, sha256=checksum, size=size,
            ))
            self.db.commit()
        except BaseException:
            self.db.connection.rollback()
            raise BotApiError("Unresolved Bot API send intent; manual reconciliation required") from None

    def clear_rejected_send(self, account: str, chat_id: int, object_id: str,
                            ordinal: int, checksum: str, size: int) -> None:
        """Discard only the matching intent after explicit non-acceptance."""
        predicates = self._intent_predicate(account, chat_id, object_id, ordinal)
        self.db.connection.execute(delete(_bot_send_intents).where(
            *predicates,
            _bot_send_intents.c.sha256 == checksum,
            _bot_send_intents.c.size == size,
        ))
        self.db.commit()

    def unresolved_count(self) -> int:
        return int(self.db.connection.execute(
            select(func.count()).select_from(_bot_send_intents)
        ).scalar_one())

    def unresolved_details(self, *, limit: int = 100) -> list[dict]:
        """Read-only, bounded operator inventory for lost Bot ACK triage.

        This returns only SQL metadata, never a token, HTTP request or file ID.
        A row is NOT proof that Telegram accepted the message, and this method
        must not clear intents or cause remote-media writes.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid unresolved Bot inventory limit")
        query = select(
            _bot_send_intents.c.account,
            _bot_send_intents.c.chat_id,
            _bot_send_intents.c.object_id,
            _bot_send_intents.c.ordinal,
            _bot_send_intents.c.sha256,
            _bot_send_intents.c.size,
        ).order_by(
            _bot_send_intents.c.account,
            _bot_send_intents.c.chat_id,
            _bot_send_intents.c.object_id,
            _bot_send_intents.c.ordinal,
        ).limit(limit)
        return [dict(row) for row in self.db.connection.execute(query).mappings()]

    def put_receipt(self, account: str, chat_id: int, message_id: int,
                    file_id: str, *, object_id: str, ordinal: int,
                    checksum: str, size: int) -> None:
        """Commit the file ID and upload receipt atomically after Telegram ACK."""
        if (not object_id or len(object_id) > 36 or ordinal < 0 or size <= 0
                or len(checksum) != 64
                or any(c not in "0123456789abcdef" for c in checksum)):
            raise ValueError("Invalid Bot upload receipt")
        try:
            with self.db.connection.begin_nested():
                self.db.connection.execute(insert(_bot_files).values(
                    account=account, chat_id=chat_id,
                    message_id=message_id, file_id=file_id,
                ))
                self.db.connection.execute(insert(_bot_receipts).values(
                    account=account, chat_id=chat_id, message_id=message_id,
                    object_id=object_id, ordinal=ordinal,
                    sha256=checksum, size=size,
                ))
                self.db.connection.execute(delete(_bot_send_intents).where(
                    *self._intent_predicate(account, chat_id, object_id, ordinal),
                    _bot_send_intents.c.sha256 == checksum,
                    _bot_send_intents.c.size == size,
                ))
            self.db.commit()
        except BaseException:
            self.db.connection.rollback()
            raise

    def find_receipt(self, object_id: str, ordinal: int, *,
                     account: str, chat_id: int, checksum: str,
                     size: int) -> ChunkReference | None:
        """Return only one exact known ACK, never guess between duplicates.

        The caller MUST read back and compare Telegram bytes against its
        immutable source before using the receipt as a chunk journal entry.
        """
        rows = self.db.connection.execute(
            select(_bot_receipts).where(
                _bot_receipts.c.object_id == object_id,
                _bot_receipts.c.ordinal == ordinal,
            ).limit(2)
        ).mappings().all()
        if not rows:
            # A crash before the ACK receipt transaction or a lost network
            # response may have left a real channel post. Never guess or
            # resend solely because the receipt cannot be found.
            ambiguous = self.db.connection.execute(
                select(_bot_send_intents.c.sha256).where(
                    *self._intent_predicate(account, chat_id, object_id, ordinal),
                ).limit(1)
            ).scalar_one_or_none()
            if ambiguous is not None:
                raise BotApiError("Unresolved Bot API send intent; manual reconciliation required")
            return None
        if len(rows) != 1:
            raise BotApiError("Ambiguous Bot API upload receipts; manual reconciliation required")
        receipt = rows[0]
        if (receipt["account"] != account or int(receipt["chat_id"]) != chat_id
                or receipt["sha256"] != checksum or int(receipt["size"]) != size):
            raise BotApiError("Bot API upload receipt does not match source chunk")
        # A deleted/unindexed file ID is never a recovery candidate.
        self.get(account, chat_id, int(receipt["message_id"]))
        return ChunkReference(
            ordinal=ordinal, offset=0, size=size, sha256=checksum,
            account=account, chat_id=chat_id,
            message_id=int(receipt["message_id"]),
        )

    def unjournaled_count(self) -> int:
        """Count Bot ACKs not referenced by any manifest chunk journal.

        Multipart completion may reparent a chunk to another object ID:
        join by the immutable Telegram message identity, not object ID.
        This inventory is non-destructive and includes in-flight ACKs.
        """
        count = self.db.connection.execute(
            select(func.count()).select_from(
                _bot_receipts.outerjoin(chunks, and_(
                    chunks.c.account == _bot_receipts.c.account,
                    chunks.c.chat_id == _bot_receipts.c.chat_id,
                    chunks.c.message_id == _bot_receipts.c.message_id,
                ))
            ).where(chunks.c.object_id.is_(None))
        ).scalar_one()
        return int(count)

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
        conditions = (
            _bot_files.c.account == account,
            _bot_files.c.chat_id == chat_id,
            _bot_files.c.message_id == message_id,
        )
        receipt_conditions = (
            _bot_receipts.c.account == account,
            _bot_receipts.c.chat_id == chat_id,
            _bot_receipts.c.message_id == message_id,
        )
        try:
            with self.db.connection.begin_nested():
                self.db.connection.execute(delete(_bot_receipts).where(*receipt_conditions))
                self.db.connection.execute(delete(_bot_files).where(*conditions))
            self.db.commit()
        except BaseException:
            self.db.connection.rollback()
            raise


class _TelegramHTTPSRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow an untrusted or plaintext redirect containing bot credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (parsed.scheme.lower() != "https"
                or parsed.hostname != "api.telegram.org"
                or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None):
            raise BotApiError("Telegram redirected to a disallowed destination")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _telegram_https_opener():
    # Use a private opener, rather than modifying urllib's global handlers.
    return urllib.request.build_opener(_TelegramHTTPSRedirects())


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
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8"))
        output.extend((
            f"--{boundary}\r\nContent-Disposition: form-data; "
            'name="document"; filename="chunk.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii"))
        output.extend(source.read_bytes())
        output.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
        return bytes(output), f"multipart/form-data; boundary={boundary}"

    def _call_sync(self, method: str, fields: dict[str, Any],
                   source: Path | None = None) -> dict[str, Any]:
        if source is not None:
            body, content_type = self._multipart(fields, source)
        else:
            body = json.dumps(fields).encode("utf-8")
            content_type = "application/json"
        url = f"https://api.telegram.org/bot{self.__token}/{method}"
        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "api.telegram.org":
            raise BotApiError("Bot API requests must use Telegram HTTPS")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": content_type}, method="POST"
        )
        try:
            with _telegram_https_opener().open(request, timeout=90) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise BotApiError(f"Bot API {method} response exceeded size limit")
            parsed = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Telegram returns retry_after on HTTP 429, normally in a JSON
            # error body. Only a positively identified rejection may retry.
            if exc.code == 429:
                try:
                    response = json.loads(exc.read(8192))
                    seconds = response.get("parameters", {}).get("retry_after")
                except (ValueError, TypeError, AttributeError, OSError):
                    seconds = None
                if type(seconds) is int and 1 <= seconds <= 3600:
                    raise BotApiRateLimited(seconds) from None
            raise BotApiError(f"Bot API {method} request failed") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise BotApiError(f"Bot API {method} request failed") from None
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            code = parsed.get("error_code", "unknown") if isinstance(parsed, dict) else "unknown"
            if code == 429 and isinstance(parsed, dict):
                params = parsed.get("parameters")
                seconds = params.get("retry_after") if isinstance(params, dict) else None
                if type(seconds) is int and 1 <= seconds <= 3600:
                    raise BotApiRateLimited(seconds)
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
        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "api.telegram.org":
            raise BotApiError("Bot file downloads must use Telegram HTTPS")
        created = False
        try:
            request = urllib.request.Request(url)
            with _telegram_https_opener().open(request, timeout=90) as response:
                with destination.open("xb") as output:
                    created = True
                    size = 0
                    while data := response.read(1024 * 1024):
                        size += len(data)
                        if size > BOT_DOWNLOAD_LIMIT:
                            raise BotApiError("Telegram bot download exceeds 20 MiB")
                        output.write(data)
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as exc:
            if created:
                destination.unlink(missing_ok=True)
            raise BotApiError("Bot API file download failed") from None
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise

    async def download_file(self, file_path: str, destination: Path) -> None:
        await asyncio.to_thread(self._download_sync, file_path, destination)


class BotApiChunkTransport:
    """TelegramTransport compatible adapter for a single bot/channel pairing."""

    def __init__(self, api: Any, index: BotFileIndex, *,
                 chat_id: int, account: str = "bot",
                 min_upload_interval: float = 1.1):
        if not account or not isinstance(chat_id, int) or chat_id == 0:
            raise ValueError("A bot account alias and numeric channel ID are required")
        if not 0 <= min_upload_interval <= 60:
            raise ValueError("Invalid Telegram bot upload interval")
        self.api, self.index, self.chat_id, self.account = api, index, chat_id, account
        self._min_upload_interval = min_upload_interval
        self._upload_lock = asyncio.Lock()
        self._next_upload = 0.0

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
        size = path.stat().st_size
        self.index.start_send(
            self.account, self.chat_id, object_id, ordinal, checksum, size,
        )
        # Telegram advises roughly one message per second per chat for bots.
        # Serializing uploads on this transport avoids self-inflicted 429s.
        async with self._upload_lock:
            # One bounded retry is safe ONLY after an explicit Telegram 429:
            # it proves the previous send was rejected. An unknown network
            # failure can mean an ACK was lost after upload; never retry it.
            for attempt in range(2):
                pause = max(0.0, self._next_upload - time.monotonic())
                if pause:
                    await asyncio.sleep(pause)
                try:
                    message = await self.api.call("sendDocument", {
                        "chat_id": self.chat_id,
                        "caption": f"teledrive:{object_id}:{ordinal}:{checksum[:16]}",
                    }, source=path)
                    break
                except BotApiRateLimited as exc:
                    self._next_upload = time.monotonic() + max(
                        self._min_upload_interval, exc.retry_after,
                    )
                    if attempt or exc.retry_after > 60:
                        # Explicit HTTP 429 means Telegram rejected this
                        # request. Clearing the intent is safe; lost ACKs are
                        # NOT equivalent to a definite rejection.
                        self.index.clear_rejected_send(
                            self.account, self.chat_id, object_id,
                            ordinal, checksum, size,
                        )
                        raise
                finally:
                    self._next_upload = max(
                        self._next_upload,
                        time.monotonic() + self._min_upload_interval,
                    )
        try:
            mid = int(message["message_id"])
            chat = int(message["chat"]["id"])
            fid = str(message["document"]["file_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BotApiError("Bot API did not return a document ID") from None
        if chat != self.chat_id or mid <= 0 or not fid:
            raise BotApiError("Bot API returned an unexpected upload target or file ID")
        self.index.put_receipt(
            self.account, chat, mid, fid,
            object_id=object_id, ordinal=ordinal, checksum=checksum,
            size=size,
        )
        return self.account, chat, mid

    def recover_receipt(self, object_id: str, ordinal: int,
                        checksum: str, size: int) -> ChunkReference | None:
        """Candidate only: resume_file independently verifies its Telegram bytes."""
        return self.index.find_receipt(
            object_id, ordinal, account=self.account, chat_id=self.chat_id,
            checksum=checksum, size=size,
        )

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
