"""Telethon-backed chunk I/O.

Sessions must already be authorized. Telegram is the only payload backend;
this adapter does not silently fall back to a local/S3 storage provider.
"""
from __future__ import annotations

from pathlib import Path
import re
from uuid import UUID
from typing import Any, Mapping

from .manifest import ChunkReference
from .recovery import Discovery, RemoteChunk


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


    @staticmethod
    def _parse_marker(caption: str | None, object_id: str) -> tuple[int, str] | None:
        # Never accept substrings or prefixes, only the exact upload marker.
        try:
            UUID(object_id)
        except ValueError:
            return None
        matched = re.fullmatch(
            rf"teledrive:{re.escape(object_id)}:([0-9]{{1,10}}):([0-9a-f]{{16}})",
            caption or "",
        )
        if matched is None:
            return None
        return int(matched.group(1)), matched.group(2)

    async def _message_for(self, part: RemoteChunk):
        account = self.accounts.get(part.account)
        if account is None:
            raise RuntimeError("Stored Telegram account is not configured")
        client, target = account
        peer_id = int(await client.get_peer_id(target))
        if peer_id != part.chat_id:
            # Do not inspect or delete a message from a different target.
            raise RuntimeError("Configured Telegram target differs from the original chunk")
        return client, target, await client.get_messages(target, ids=part.message_id)

    async def probe(self, chunk: RemoteChunk, *, object_id: str) -> str:
        _, _, message = await self._message_for(chunk)
        if message is None:
            return "missing"
        marker = self._parse_marker(getattr(message, "message", None), object_id)
        actual_chat = getattr(message, "chat_id", None)
        if (not getattr(message, "out", False) or marker is None
                or marker != (chunk.ordinal, chunk.checksum_prefix)
                or (actual_chat is not None and int(actual_chat) != chunk.chat_id)):
            return "mismatch"
        return "verified"

    async def discover(self, object_id: str, *, scan_limit: int) -> Discovery:
        if scan_limit < 1:
            raise ValueError("scan_limit must be positive")
        found: list[RemoteChunk] = []
        scanned = 0
        complete = True
        for name, (client, target) in self.accounts.items():
            chat_id = int(await client.get_peer_id(target))
            local_count = 0
            async for message in client.iter_messages(target, limit=scan_limit + 1):
                local_count += 1
                scanned += 1
                if local_count > scan_limit:
                    complete = False
                    break
                marker = self._parse_marker(getattr(message, "message", None), object_id)
                if marker is None:
                    continue
                # Channel messages by other admins may look like uploads but
                # cannot be proven owned by this TeleDrive account.
                if not getattr(message, "out", False):
                    complete = False
                    continue
                remote_chat = getattr(message, "chat_id", None)
                if remote_chat is not None and int(remote_chat) != chat_id:
                    complete = False
                    continue
                found.append(RemoteChunk(name, chat_id, int(message.id), *marker))
        return Discovery(tuple(found), complete, scanned)

    async def delete_verified(self, chunk: RemoteChunk, *, object_id: str) -> bool:
        if await self.probe(chunk, object_id=object_id) != "verified":
            return False
        client, target, _ = await self._message_for(chunk)
        await client.delete_messages(target, [chunk.message_id], revoke=True)
        # Do not assume Telegram accepted deletion. A later retry can safely
        # continue if eventual consistency makes the message still visible.
        return await self.probe(chunk, object_id=object_id) == "missing"
