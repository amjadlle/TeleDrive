"""Telethon-backed chunk I/O.

Sessions must already be authorized. Telegram is the only payload backend;
this adapter does not silently fall back to a local/S3 storage provider.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .manifest import ChunkReference


class TelethonChunkTransport:
    """Use configured account clients for writes and original account for reads.

    Each mapping value is (connected Telethon client, Telegram target entity).
    The application must keep the sessions connected while requests run.
    """

    def __init__(self, accounts: Mapping[str, tuple[Any, Any]]):
        if not accounts:
            raise ValueError("At least one authorized Telegram account is required")
        self.accounts = dict(accounts)
        self._cursor = 0

    async def upload(self, path: Path, *, object_id: str, ordinal: int,
                     checksum: str) -> tuple[str, int, int]:
        names = list(self.accounts)
        account = names[self._cursor % len(names)]
        self._cursor += 1
        client, target = self.accounts[account]
        # A stable object/part marker allows later orphan reconciliation after
        # send_file succeeded but DB commit failed or the process was killed.
        caption = f"teledrive:{object_id}:{ordinal}:{checksum[:16]}"
        message = await client.send_file(
            target, str(path), caption=caption, force_document=True
        )
        if message is None or not getattr(message, "id", None):
            raise RuntimeError("Telegram did not return a valid message")
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None:
            chat_id = await client.get_peer_id(target)
        return account, int(chat_id), int(message.id)

    async def download(self, reference: ChunkReference, destination: Path) -> None:
        account = self.accounts.get(reference.account)
        if account is None:
            raise FileNotFoundError("Telegram account holding this chunk is unavailable")
        client, target = account
        # Re-fetch the original message (rather than caching file_reference).
        # Reuse the already-resolved target where it matches the saved chat ID;
        # StringSession does not necessarily persist Telegram entity caches.
        peer = reference.chat_id
        if int(await client.get_peer_id(target)) == reference.chat_id:
            peer = target
        message = await client.get_messages(peer, ids=reference.message_id)
        if message is None or not getattr(message, "media", None):
            raise FileNotFoundError("Telegram message/media was deleted or inaccessible")
        result = await client.download_media(message, file=str(destination))
        if result is None:
            raise FileNotFoundError("Telegram media download failed")
        actual = Path(result)
        if actual != destination:
            # Telethon may append a MIME extension to an extensionless target.
            if actual.resolve().parent != destination.parent.resolve():
                raise RuntimeError("Unexpected Telegram download path")
            actual.replace(destination)
        if not destination.is_file() or destination.is_symlink():
            raise FileNotFoundError("Telegram media download failed")

    async def delete(self, reference: ChunkReference) -> None:
        """Explicit cleanup hook; never run automatically before reconciliation."""
        account = self.accounts.get(reference.account)
        if account is None:
            raise FileNotFoundError("Telegram account unavailable for cleanup")
        client, _ = account
        await client.delete_messages(reference.chat_id, [reference.message_id])
