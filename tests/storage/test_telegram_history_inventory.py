"""Read-only Telegram history discovery remains bounded and unverified."""
from __future__ import annotations

import asyncio
import stat
from types import SimpleNamespace
from uuid import uuid4

import pytest

from teledrive.storage.history_inventory import (
    inventory_channel, parse_candidate_marker, write_private_report,
)


def marker(object_id, ordinal, prefix="a" * 16):
    return f"teledrive:{object_id}:{ordinal}:{prefix}"


def message(mid, caption, *, out=True, chat_id=-100123, media=True):
    return SimpleNamespace(
        id=mid, message=caption, out=out, chat_id=chat_id,
        media=object() if media else None,
    )


class FakeTelegram:
    def __init__(self, messages, *, fail_after=None):
        self.messages = messages
        self.fail_after = fail_after
        self.calls = []

    async def get_peer_id(self, target):
        self.calls.append(("get_peer_id", target))
        return -100123

    async def iter_messages(self, target, *, limit):
        self.calls.append(("iter_messages", target, limit))
        for index, item in enumerate(self.messages[:limit]):
            if index == self.fail_after:
                raise RuntimeError("Lost Telegram history read")
            yield item


def scan(messages, *, limit=100):
    client = FakeTelegram(messages)
    report = asyncio.run(inventory_channel(
        client, "authorized-channel", account="original", scan_limit=limit,
    ))
    return report, client


def test_valid_markers_are_candidates_not_restorable_manifests():
    ident = str(uuid4())
    report, client = scan([
        message(105, marker(ident, 1, "b" * 16)),
        message(104, marker(ident, 0)),
        message(103, "ordinary channel post"),
    ])
    assert report["scanned_messages"] == 3
    assert report["candidate_object_count"] == 1
    assert report["candidate_chunk_count"] == 2
    assert report["readable_window_exhausted"] and not report["truncated"]
    group = report["candidate_objects"][0]
    assert [p["ordinal"] for p in group["chunks"]] == [0, 1]
    assert [p["message_id"] for p in group["chunks"]] == [104, 105]
    assert group["candidate_ordinals_contiguous"]
    assert group["bucket"] is None and group["key"] is None
    assert group["whole_object_sha256"] is None
    assert not group["payload_verified"] and not group["original_mapping_verified"]
    assert report["complete_unknown_history_discovered"] is False
    assert report["restorable_without_independent_manifest"] is False
    assert report["payload_downloads"] == report["telegram_mutations"] == 0
    assert client.calls == [("get_peer_id", "authorized-channel"),
                            ("iter_messages", "authorized-channel", 101)]


def test_wrong_owner_channel_or_media_is_not_a_candidate():
    ident = str(uuid4())
    report, _ = scan([
        message(1, marker(ident, 0), out=False),
        message(2, marker(ident, 0), chat_id=-100999),
        message(3, marker(ident, 0), media=False),
        message(4, marker(ident, 0)),
        message(5, marker(ident, 1) + "extra"),
        message(6, "other:" + marker(ident, 1)),
    ])
    assert report["rejected_marker_messages"] == 3
    assert report["candidate_chunk_count"] == 1
    assert report["candidate_objects"][0]["chunks"][0]["message_id"] == 4


@pytest.mark.parametrize("caption", [
    None, "", "teledrive:no-uuid:0:" + "a" * 16,
    "prefix:teledrive:" + str(uuid4()) + ":0:" + "a" * 16,
    marker(str(uuid4()).upper(), 0),
    marker(str(uuid4()), 0, "x" * 16),
    marker(str(uuid4()), 0) + "\n",
])
def test_ambiguous_markers_are_rejected(caption):
    assert parse_candidate_marker(caption) is None


def test_bounded_scan_reports_truncation_and_never_guesses_missing_parts():
    ident = str(uuid4())
    report, _ = scan([
        message(9, marker(ident, 7)),
        message(8, marker(ident, 0)),
        message(7, marker(ident, 1)),
    ], limit=2)
    assert report["scanned_messages"] == 2
    assert report["truncated"] is True
    assert report["readable_window_exhausted"] is False
    group = report["candidate_objects"][0]
    assert group["candidate_ordinals_contiguous"] is False
    assert [p["ordinal"] for p in group["chunks"]] == [0, 7]


def test_duplicate_ordinals_are_flagged_not_silently_collapsed():
    ident = str(uuid4())
    report, _ = scan([
        message(9, marker(ident, 0)),
        message(8, marker(ident, 0, "b" * 16)),
    ])
    group = report["candidate_objects"][0]
    assert group["duplicate_ordinals"] is True
    assert group["candidate_ordinals_contiguous"] is False
    assert len(group["chunks"]) == 2


@pytest.mark.parametrize("limit", [0, -1, 10_001, True, 1.5, "5"])
def test_invalid_scan_limits_fail_before_network(limit):
    client = FakeTelegram([])
    with pytest.raises(ValueError, match="scan_limit"):
        asyncio.run(inventory_channel(
            client, "authorized-channel", account="original", scan_limit=limit,
        ))
    assert client.calls == []


def test_failed_remote_iteration_does_not_return_partial_inventory():
    ident = str(uuid4())
    client = FakeTelegram([message(1, marker(ident, 0)),
                           message(2, marker(ident, 1))], fail_after=1)
    with pytest.raises(RuntimeError, match="Lost Telegram"):
        asyncio.run(inventory_channel(
            client, "authorized-channel", account="original",
        ))


def test_duplicate_message_identity_fails_closed():
    ident = str(uuid4())
    with pytest.raises(ValueError, match="Duplicate Telegram"):
        scan([message(3, marker(ident, 0)),
              message(3, marker(ident, 1))])


def test_private_report_uses_exclusive_creation_and_preserves_existing(tmp_path):
    report = {"candidate_objects": [{"message_id": 123}]}
    path = tmp_path / "inventory.json"
    write_private_report(path, report)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_private_report(path, {"different": True})
    assert path.read_bytes() == original


def test_private_report_rejects_symlink_and_missing_directory(tmp_path):
    target = tmp_path / "existing.json"
    target.write_text("untouched", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        write_private_report(link, {})
    assert target.read_text(encoding="utf-8") == "untouched"
    with pytest.raises(FileNotFoundError):
        write_private_report(tmp_path / "missing" / "inventory.json", {})


def test_paginated_read_only_history_and_external_markers():
    ident = str(uuid4())
    bot_ident = str(uuid4())
    foreign = message(99, marker(bot_ident, 0), out=False)
    telegram = FakeTelegram([
        message(103, "general channel post"),
        message(102, marker(ident, 0)),
        message(101, "another unrelated post"),
        message(100, marker(ident, 1)),
        foreign,
    ])
    async def first():
        return await inventory_channel(
            telegram, "authorized-channel", account="owner",
            scan_limit=2, include_external_markers=True,
        )
    first_page = asyncio.run(first())
    assert first_page["truncated"]
    assert first_page["next_before_message_id"] == 102
    assert first_page["external_marker_candidate_count"] == 0
    assert first_page["candidate_chunk_count"] == 1
    assert telegram.calls[-1] == ("iter_messages", "authorized-channel", 3)

    class PagedTelegram(FakeTelegram):
        async def iter_messages(self, target, *, limit, max_id=None):
            self.calls.append(("iter_messages", target, limit, max_id))
            for item in self.messages:
                if max_id is None or item.id < max_id:
                    yield item
                    limit -= 1
                    if limit == 0:
                        break

    paged = PagedTelegram(telegram.messages)
    other = asyncio.run(inventory_channel(
        paged, "authorized-channel", account="owner", scan_limit=3,
        before_message_id=first_page["next_before_message_id"],
        include_external_markers=True,
    ))
    assert other["scanned_messages"] == 3
    assert other["truncated"] is False
    assert other["candidate_chunk_count"] == 1
    assert other["external_marker_candidate_count"] == 1
    candidate = other["external_marker_candidates"][0]
    assert candidate["object_id"] == bot_ident
    assert candidate["message_id"] == 99
    assert candidate["ownership_verified"] is False
    assert candidate["payload_verified"] is False
    assert candidate["safe_to_publish_or_delete"] is False
    assert other["restorable_without_independent_manifest"] is False
    assert other["telegram_mutations"] == other["payload_downloads"] == 0
    assert paged.calls[-1] == ("iter_messages", "authorized-channel", 4, 102)


@pytest.mark.parametrize("before", [0, -1, "42", True, 1.5])
def test_invalid_history_cursor_fails_before_network(before):
    client = FakeTelegram([])
    with pytest.raises(ValueError, match="continuation"):
        asyncio.run(inventory_channel(
            client, "authorized-channel", account="owner",
            before_message_id=before,
        ))
    assert client.calls == []
