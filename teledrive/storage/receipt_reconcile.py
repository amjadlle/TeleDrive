"""Operator-gated Bot ACK recovery with independent Telegram channel proof.

A Bot API response can be lost after a successful sendDocument. It cannot
recover historical channel messages by itself. A second authenticated
Telethon account therefore verifies the *exact* channel post and content,
while Bot getFile independently verifies the operator-provided file ID.
No send, delete, or SQL mutation happens until explicit apply.
"""
from __future__ import annotations

import hashlib
import hmac
import tempfile
from pathlib import Path
from typing import Any

from teledrive.storage.bot_api_adapter import (
    BOT_DOWNLOAD_LIMIT, BotApiError, BotFileIndex,
)


def _digest(path: Path) -> tuple[int, str]:
    size = 0
    sha = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(1024 * 1024):
            size += len(block)
            if size > BOT_DOWNLOAD_LIMIT:
                raise BotApiError("Recovery evidence exceeds safe Bot download limit")
            sha.update(block)
    return size, sha.hexdigest()


async def reconcile_bot_receipt(
    index: BotFileIndex, bot: Any, telethon: Any, *,
    account: str, chat_id: int, object_id: str, ordinal: int,
    message_id: int, file_id: str, apply: bool = False,
) -> dict:
    """Validate two independently downloaded copies against the SQL intent.

    The operator MUST supply a trustworthy Bot file ID from retained evidence;
    Telegram's Bot API cannot list channel history and cannot derive an
    arbitrary old file ID from a channel message ID. This function verifies
    both the specific channel message and file ID contents before an
    operator-authorized receipt commit. It never uploads or deletes media.
    """
    if (not account or not isinstance(object_id, str)
            or not isinstance(file_id, str) or not file_id
            or len(file_id) > 4096 or type(chat_id) is not int
            or not chat_id or type(ordinal) is not int or ordinal < 0
            or type(message_id) is not int or message_id <= 0
            or type(apply) is not bool):
        raise ValueError("Invalid Telegram reconciliation identity")
    intent = index.get_send_intent(
        account=account, chat_id=chat_id,
        object_id=object_id, ordinal=ordinal,
    )
    if intent is None:
        raise BotApiError("No outstanding pre-send intent to reconcile")
    expected_size = intent["size"]
    expected_hash = intent["sha256"]
    if not 0 < expected_size <= BOT_DOWNLOAD_LIMIT:
        raise BotApiError("Invalid stored source size")
    with tempfile.TemporaryDirectory(prefix="teledrive-proof-") as dirname:
        root = Path(dirname)
        bot_meta = await bot.call("getFile", {"file_id": file_id})
        remote_path = bot_meta.get("file_path")
        if not isinstance(remote_path, str):
            raise BotApiError("Bot file identifier is no longer retrievable")
        if "file_size" in bot_meta and bot_meta["file_size"] != expected_size:
            raise BotApiError("Bot file size contradicts original upload intent")
        bot_copy = root / "bot-file"
        await bot.download_file(remote_path, bot_copy)
        bot_size, bot_sha = _digest(bot_copy)
        if bot_size != expected_size or not hmac.compare_digest(bot_sha, expected_hash):
            raise BotApiError("Bot file bytes contradict original upload intent")

        # Independently read channel history with a user session; Bot API
        # getFile alone cannot verify that a particular post exists there.
        channel_message = await telethon.get_messages(chat_id, ids=message_id)
        if (channel_message is None
                or type(getattr(channel_message, "id", None)) is not int
                or channel_message.id != message_id
                or getattr(channel_message, "chat_id", None) != chat_id
                or getattr(channel_message, "document", None) is None
                or getattr(channel_message.document, "size", None) != expected_size
                or getattr(channel_message, "raw_text", None)
                != f"teledrive:{object_id}:{ordinal}:{expected_hash[:16]}"):
            raise BotApiError("Independent channel message proof does not match intent")
        channel_copy = root / "channel-message"
        result = await telethon.download_media(
            channel_message, file=str(channel_copy),
        )
        if not result or not channel_copy.is_file():
            raise BotApiError("Cannot download independent channel message proof")
        channel_size, channel_sha = _digest(channel_copy)
        if (channel_size != expected_size
                or not hmac.compare_digest(channel_sha, expected_hash)):
            raise BotApiError("Channel message bytes contradict original upload intent")
    if apply:
        # A different worker can have resolved the intent meanwhile. Recheck
        # the immutable identity inside the index's receipt transaction.
        index.put_receipt(
            account, chat_id, message_id, file_id,
            object_id=object_id, ordinal=ordinal,
            checksum=expected_hash, size=expected_size,
            require_intent=True,
        )
    return {
        "verified": True, "applied": apply,
        "account": account, "chat_id": chat_id, "object_id": object_id,
        "ordinal": ordinal, "message_id": message_id,
        "sha256": expected_hash, "size": expected_size,
        "remote_uploads": 0, "remote_deletions": 0,
    }
