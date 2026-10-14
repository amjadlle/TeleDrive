"""Conservative recovery and garbage collection for failed Telegram uploads.

No Telegram messages are deleted automatically. The caller must explicitly
request cleanup, pass a minimum age gate, and provide a transport able to
prove the messages belong to that exact failed object.

Ready and in-progress objects are never garbage-collected by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Literal, Protocol

from sqlalchemy import select, update

from .manifest import ChunkReference, ManifestError, ManifestStore, chunks, objects


class UnsafeCleanup(ManifestError):
    """An incomplete or ambiguous scan prevents any destructive action."""


@dataclass(frozen=True)
class RemoteChunk:
    account: str
    chat_id: int
    message_id: int
    ordinal: int
    checksum_prefix: str

    @property
    def identity(self) -> tuple[str, int, int]:
        return self.account, self.chat_id, self.message_id


@dataclass(frozen=True)
class Discovery:
    matches: tuple[RemoteChunk, ...]
    complete: bool
    scanned: int


@dataclass(frozen=True)
class CleanupReport:
    object_id: str
    journaled: int
    discovered: int
    orphaned: int
    missing: int
    deleted: int
    dry_run: bool
    cleaned: bool


class RecoveryTransport(Protocol):
    async def discover(self, object_id: str, *, scan_limit: int) -> Discovery:
        """Scan known targets for outgoing uploads with an exact object marker."""
        ...

    async def probe(self, chunk: RemoteChunk, *, object_id: str) -> Literal["verified", "missing", "mismatch"]:
        """Prove outgoing message caption matches this object, ordinal, digest prefix."""
        ...

    async def delete_verified(self, chunk: RemoteChunk, *, object_id: str) -> bool:
        """Delete only after verifying ownership; return True only if absent afterward."""
        ...


def _from_journal(part: ChunkReference) -> RemoteChunk:
    return RemoteChunk(
        account=part.account, chat_id=part.chat_id, message_id=part.message_id,
        ordinal=part.ordinal, checksum_prefix=part.sha256[:16],
    )


class RecoveryManager:
    def __init__(self, manifests: ManifestStore, transport: RecoveryTransport, *,
                 min_age_seconds: int = 24 * 60 * 60, scan_limit: int = 10000):
        if min_age_seconds < 60 or scan_limit < 1:
            raise ValueError("Unsafe recovery age/scan limits")
        self.manifests = manifests
        self.transport = transport
        self.min_age_seconds = min_age_seconds
        self.scan_limit = scan_limit

    def _eligible(self, object_id: str, now: int) -> None:
        row = self.manifests.db.connection.execute(
            select(objects.c.status, objects.c.updated_at).where(objects.c.id == object_id)
        ).first()
        if row is None:
            raise FileNotFoundError("Object not found")
        if row[0] != "failed":
            raise UnsafeCleanup("Only failed objects may be cleaned up")
        if int(row[1]) > now - self.min_age_seconds:
            raise UnsafeCleanup("Failure has not aged beyond the cleanup safety interval")

    async def cleanup(self, object_id: str, *, dry_run: bool = True) -> CleanupReport:
        """Reconcile journal+Telegram, optionally delete; must run with uploader stopped.

        Acquires a per-object DB lease to serialize cleanup workers, and
        checks readiness/age both before and after scanning. If a Telegram
        target is not exhaustively scanned, no messages are deleted.
        """
        now = int(time.time())
        self._eligible(object_id, now)
        db = self.manifests.db
        lease_name = f"storage-gc:{object_id}"
        holder = f"cleanup-{object_id}-{time.time_ns()}"
        if not db.acquire_lease(lease_name, holder, now, 300):
            raise UnsafeCleanup("Another cleanup worker owns this object")
        try:
            self._eligible(object_id, now)
            journal = [_from_journal(p) for p in self.manifests.recovery_parts(object_id)]
            # Every journaled reference must either be missing or be verified.
            # If it points to unrelated Telegram media, refuse ALL deletion.
            missing = 0
            for part in journal:
                verdict = await self.transport.probe(part, object_id=object_id)
                if verdict == "mismatch":
                    raise UnsafeCleanup("Journaled message does not belong to this object")
                if verdict == "missing":
                    missing += 1
                if verdict not in ("verified", "missing", "mismatch"):
                    raise UnsafeCleanup("Transport returned an invalid proof")

            found = await self.transport.discover(object_id, scan_limit=self.scan_limit)
            if not found.complete:
                raise UnsafeCleanup("Telegram scan was truncated or incomplete")
            all_refs = {part.identity: part for part in journal}
            journal_keys = set(all_refs)
            for part in found.matches:
                if part.identity in all_refs and all_refs[part.identity] != part:
                    raise UnsafeCleanup("Conflicting Telegram chunk metadata")
                all_refs[part.identity] = part
            # A forged/malformed discovery must not cause arbitrary deletion.
            for part in found.matches:
                if await self.transport.probe(part, object_id=object_id) != "verified":
                    raise UnsafeCleanup("Discovered message ownership cannot be proved")

            orphaned = len(set(all_refs) - journal_keys)
            if dry_run:
                return CleanupReport(object_id, len(journal), len(found.matches),
                                     orphaned, missing, 0, True, False)

            self._eligible(object_id, int(time.time()))
            deleted = 0
            for part in all_refs.values():
                # Lease expiration must fail closed, never continue deleting.
                if not db.renew_lease(lease_name, holder, int(time.time()), 300):
                    raise UnsafeCleanup("Cleanup lease lost")
                verdict = await self.transport.probe(part, object_id=object_id)
                if verdict == "missing":
                    continue
                if verdict != "verified":
                    raise UnsafeCleanup("Telegram ownership changed during cleanup")
                if not await self.transport.delete_verified(part, object_id=object_id):
                    raise UnsafeCleanup("Telegram deletion not verified; cleanup can be retried")
                deleted += 1

            after = await self.transport.discover(object_id, scan_limit=self.scan_limit)
            if not after.complete or after.matches:
                raise UnsafeCleanup("Residual Telegram messages remain after cleanup")
            if not db.renew_lease(lease_name, holder, int(time.time()), 300):
                raise UnsafeCleanup("Cleanup lease lost during final scan")
            self._eligible(object_id, int(time.time()))

            # Do not publish a tombstone if state was changed by another actor.
            result = db.connection.execute(
                update(objects).where(
                    objects.c.id == object_id, objects.c.status == "failed"
                ).values(status="cleaned", updated_at=int(time.time())))
            db.commit()
            if result.rowcount != 1:
                raise UnsafeCleanup("Object state changed during cleanup")
            return CleanupReport(object_id, len(journal), len(found.matches),
                                 orphaned, missing, deleted, False, True)
        finally:
            db.release_lease(lease_name, holder)
