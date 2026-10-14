"""Lost Bot ACK proof must use two independent remote reads, never a guess."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotApiError, BotFileIndex
from teledrive.storage.receipt_reconcile import reconcile_bot_receipt


class FakeBot:
    def __init__(self, body):
        self.body = body
        self.reads = 0
        self.writes = 0

    async def call(self, method, fields):
        assert method == "getFile"
        assert fields == {"file_id": "verified-file"}
        self.reads += 1
        return {"file_path": "documents/verified-file.bin",
                "file_size": len(self.body)}

    async def download_file(self, path, destination):
        assert path == "documents/verified-file.bin"
        self.reads += 1
        destination.write_bytes(self.body)


class FakeUser:
    def __init__(self, body, *, chat_id=-10043210, message_id=11, has_document=True):
        self.body = body
        self.chat_id = chat_id
        self.message_id = message_id
        self.has_document = has_document
        self.reads = 0

    async def get_messages(self, chat_id, *, ids):
        self.reads += 1
        return SimpleNamespace(
            id=self.message_id, chat_id=self.chat_id,
            document=SimpleNamespace(size=len(self.body)) if self.has_document else None,
        )

    async def download_media(self, message, *, file):
        self.reads += 1
        Path(file).write_bytes(self.body)
        return file


def new_index(tmp_path):
    db = connect_database(str(tmp_path / "proof.db"))
    idx = BotFileIndex(db)
    body = bytes(range(256)) * 19 + b"safe recovery evidence"
    digest = hashlib.sha256(body).hexdigest()
    idx.start_send("bot-proof", -10043210, "recovery-object", 0,
                   digest, len(body))
    args = dict(account="bot-proof", chat_id=-10043210,
                object_id="recovery-object", ordinal=0,
                message_id=11, file_id="verified-file")
    return db, idx, body, args


def test_lost_ack_reconciliation_defaults_to_read_only_both_remote_sources(tmp_path):
    db, idx, body, args = new_index(tmp_path)
    try:
        bot, user = FakeBot(body), FakeUser(body)
        evidence = asyncio.run(reconcile_bot_receipt(idx, bot, user, **args))
        assert evidence["verified"] is True
        assert evidence["applied"] is False
        assert evidence["remote_uploads"] == evidence["remote_deletions"] == 0
        assert bot.reads == 2 and user.reads == 2
        assert idx.unresolved_count() == 1
        with pytest.raises(BotApiError, match="Unresolved"):
            idx.find_receipt("recovery-object", 0, account="bot-proof",
                             chat_id=-10043210,
                             checksum=hashlib.sha256(body).hexdigest(),
                             size=len(body))
        proof = asyncio.run(reconcile_bot_receipt(
            idx, bot, user, **args, apply=True,
        ))
        assert proof["verified"] and proof["applied"]
        assert idx.unresolved_count() == 0
        receipt = idx.find_receipt(
            "recovery-object", 0, account="bot-proof", chat_id=-10043210,
            checksum=hashlib.sha256(body).hexdigest(), size=len(body),
        )
        assert receipt.message_id == 11
        assert idx.get("bot-proof", -10043210, 11) == "verified-file"
        with pytest.raises(BotApiError, match="No outstanding"):
            asyncio.run(reconcile_bot_receipt(
                idx, bot, user, **args, apply=True,
            ))
    finally:
        db.close()


@pytest.mark.parametrize("variant", [
    "bot-wrong-bytes", "bot-wrong-length", "channel-wrong-bytes",
    "channel-wrong-chat", "channel-wrong-id", "channel-no-document",
])
def test_lost_ack_proof_fails_closed_on_mismatch(tmp_path, variant):
    db, idx, body, args = new_index(tmp_path)
    try:
        bot, user = FakeBot(body), FakeUser(body)
        changed = bytes([body[0] ^ 255]) + body[1:]
        if variant == "bot-wrong-bytes":
            bot.body = changed
        if variant == "bot-wrong-length":
            bot.body = body[:-1]
        if variant == "channel-wrong-bytes":
            user.body = changed
        if variant == "channel-wrong-chat":
            user.chat_id = -10043211
        if variant == "channel-wrong-id":
            user.message_id = 12
        if variant == "channel-no-document":
            user.has_document = False
        with pytest.raises(BotApiError):
            asyncio.run(reconcile_bot_receipt(
                idx, bot, user, **args, apply=True,
            ))
        assert idx.unresolved_count() == 1
        with pytest.raises(FileNotFoundError):
            idx.get("bot-proof", -10043210, 11)
    finally:
        db.close()


@pytest.mark.parametrize("variant", ["account", "object_id", "ordinal",
                                      "chat_id"])
def test_reconciliation_cannot_apply_proof_to_other_intent(tmp_path, variant):
    db, idx, body, args = new_index(tmp_path)
    try:
        altered = dict(args)
        altered[variant] = {"account": "another-bot", "object_id": "other",
                            "ordinal": 1, "chat_id": -10099999}[variant]
        with pytest.raises(BotApiError, match="No outstanding"):
            asyncio.run(reconcile_bot_receipt(
                idx, FakeBot(body), FakeUser(body), **altered, apply=True,
            ))
        assert idx.unresolved_count() == 1
    finally:
        db.close()


def test_reconciliation_refuses_bad_numeric_identifiers_before_bot_api(tmp_path):
    db, idx, body, args = new_index(tmp_path)
    try:
        for name, invalid in (("message_id", -1), ("chat_id", 0),
                              ("ordinal", -1), ("message_id", True),
                              ("file_id", ""), ("apply", "yes")):
            broken = dict(args, **{name: invalid})
            with pytest.raises(ValueError):
                asyncio.run(reconcile_bot_receipt(
                    idx, FakeBot(body), FakeUser(body), **broken,
                ))
        assert idx.unresolved_count() == 1
    finally:
        db.close()
