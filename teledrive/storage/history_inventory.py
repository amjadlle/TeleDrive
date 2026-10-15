"""Bounded, read-only inventory of TeleDrive markers in one Telegram channel.

Captions are hints, NOT payload integrity, ownership, S3 key, bucket, or
version proofs. This code deliberately cannot create a rebuild plan or delete
or download Telegram media.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import UUID

from .telethon_adapter import TelethonChunkTransport

MAX_SCAN_MESSAGES = 10_000
_MARKER = re.compile(
    r"teledrive:([0-9a-f-]{36}):([0-9]{1,10}):([0-9a-f]{16})\Z"
)


def parse_candidate_marker(caption: str | None) -> tuple[str, int, str] | None:
    """Parse a canonical UUID marker with the existing transport validator."""
    if not isinstance(caption, str):
        return None
    match = _MARKER.fullmatch(caption)
    if match is None:
        return None
    object_id = match.group(1)
    try:
        if str(UUID(object_id)) != object_id:
            return None
    except ValueError:
        return None
    part = TelethonChunkTransport._parse_marker(caption, object_id)
    if part is None:
        return None
    return object_id, part[0], part[1]


async def inventory_channel(client, target, *, account: str,
                            scan_limit: int = 1000,
                            before_message_id: int | None = None,
                            include_external_markers: bool = False) -> dict:
    """Inspect at most scan_limit messages from a single resolved target.

    The extra message is used solely to detect truncation; it is not
    analyzed. Telegram iteration errors propagate: no partial report is
    returned as if the scan had succeeded.
    """
    if (type(scan_limit) is not int or not 1 <= scan_limit <= MAX_SCAN_MESSAGES):
        raise ValueError("scan_limit must be an integer between 1 and 10000")
    if (not isinstance(account, str) or not 1 <= len(account) <= 255
            or any(ord(char) < 32 or ord(char) == 127 for char in account)):
        raise ValueError("Invalid authorized account label")
    if (before_message_id is not None and
            (type(before_message_id) is not int or before_message_id < 1)):
        raise ValueError("Invalid channel-history continuation message ID")
    if type(include_external_markers) is not bool:
        raise ValueError("Invalid external-marker inventory flag")
    chat_id = int(await client.get_peer_id(target))
    groups: dict[str, list[dict]] = {}
    seen_message_ids: set[int] = set()
    scanned = 0
    rejected_markers = 0
    truncated = False
    external_markers = []
    last_message_id = None
    iterator_kwargs = {"limit": scan_limit + 1}
    if before_message_id is not None:
        # Telethon max_id is EXCLUSIVE; do not re-read the last page's tail.
        iterator_kwargs["max_id"] = before_message_id
    async for message in client.iter_messages(target, **iterator_kwargs):
        if scanned == scan_limit:
            truncated = True
            break
        scanned += 1
        mid = getattr(message, "id", None)
        if type(mid) is int and mid > 0:
            last_message_id = (mid if last_message_id is None
                               else min(last_message_id, mid))
        marker = parse_candidate_marker(getattr(message, "message", None))
        if marker is None:
            continue
        remote_chat = getattr(message, "chat_id", None)
        # The outgoing flag and chat match are prerequisites for a candidate,
        # but even together they do NOT prove independently verified ownership.
        if (not getattr(message, "out", False)
                or getattr(message, "media", None) is None
                or type(mid) is not int or mid <= 0
                or type(remote_chat) is not int or remote_chat != chat_id):
            rejected_markers += 1
            # A user can read bot/other-admin channel posts but not prove
            # authorship. Preserve only exact markers from the SAME channel
            # as explicitly UNVERIFIED hints for owner-side investigation.
            if (include_external_markers and not getattr(message, "out", False)
                    and getattr(message, "media", None) is not None
                    and type(mid) is int and mid > 0
                    and type(remote_chat) is int and remote_chat == chat_id):
                object_id, ordinal, prefix = marker
                external_markers.append({
                    "object_id": object_id, "ordinal": ordinal,
                    "checksum_prefix": prefix, "chat_id": chat_id,
                    "message_id": mid, "ownership_verified": False,
                    "payload_verified": False,
                    "safe_to_publish_or_delete": False,
                })
            continue
        if mid in seen_message_ids:
            raise ValueError("Duplicate Telegram message ID in channel scan")
        seen_message_ids.add(mid)
        object_id, ordinal, prefix = marker
        groups.setdefault(object_id, []).append({
            "ordinal": ordinal,
            "checksum_prefix": prefix,
            "account": account,
            "chat_id": chat_id,
            "message_id": mid,
        })

    if truncated and last_message_id is None:
        raise ValueError("History page cannot advance safely without message IDs")
    objects = []
    for object_id, parts in sorted(groups.items()):
        parts.sort(key=lambda item: (item["ordinal"], item["message_id"]))
        ordinals = [part["ordinal"] for part in parts]
        unique = sorted(set(ordinals))
        contiguous = (len(ordinals) == len(unique) and bool(unique)
                      and unique[0] == 0 and all(
                          current == previous + 1
                          for previous, current in zip(unique, unique[1:])
                      ))
        objects.append({
            "object_id": object_id,
            "chunks": parts,
            "candidate_ordinals_contiguous": contiguous,
            "duplicate_ordinals": len(unique) != len(ordinals),
            "bucket": None,
            "key": None,
            "version_id": None,
            "whole_object_sha256": None,
            "payload_verified": False,
            "original_mapping_verified": False,
        })
    return {
        "schema_version": 1,
        "scope": {"account": account, "chat_id": chat_id,
                  "scan_limit": scan_limit},
        "scanned_messages": scanned,
        "truncated": truncated,
        "before_message_id": before_message_id,
        "next_before_message_id": last_message_id if truncated else None,
        "external_marker_candidates": external_markers,
        "external_marker_candidate_count": len(external_markers),
        "readable_window_exhausted": not truncated,
        "rejected_marker_messages": rejected_markers,
        "candidate_objects": objects,
        "candidate_object_count": len(objects),
        "candidate_chunk_count": sum(len(obj["chunks"]) for obj in objects),
        "payload_downloads": 0,
        "telegram_mutations": 0,
        "production_database_modified": False,
        "restorable_without_independent_manifest": False,
        "complete_unknown_history_discovered": False,
    }


def write_private_report(destination: Path, report: dict) -> None:
    """Create a 0600 JSON report exactly once; never replace an old report."""
    destination = Path(destination)
    if not destination.parent.is_dir():
        raise FileNotFoundError("Report parent directory does not exist")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
