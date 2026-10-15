"""Multiple Telegram api_id/api_hash credential sets with rotation strategies.

The unified USER transport may hold several authorized sessions posting to
the SAME dedicated channel. round_robin distributes sends across sets;
failover keeps a primary set until a definite flood-wait rejection, then
switches. Ambiguous send outcomes never trigger cross-set retries.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from teledrive.db import connect_database
from teledrive.storage.user_mode import (
    UnifiedUserTransport, UserSendJournal, UserSessionRuntime,
)


def _write_session(tmp_path, name, value="fake-session-data"):
    target = tmp_path / name
    target.write_text(value + "\n", encoding="ascii")
    return target


class FakeClient:
    identity_bot = False

    def __init__(self, *, label, runtime, chat_id=-10093744, peer=-10093744):
        self.label = label
        self.runtime = runtime
        self.chat_id = chat_id
        self.peer = peer
        self.sent = 0

    async def connect(self):
        self.runtime.connected.append(self.label)

    async def disconnect(self):
        pass

    async def is_connected(self):
        return True

    async def get_me(self):
        from types import SimpleNamespace
        return SimpleNamespace(bot=type(self).identity_bot)

    async def iter_dialogs(self, limit):
        from types import SimpleNamespace
        assert limit == 5000
        from telethon.tl.types import Channel
        yield SimpleNamespace(id=self.peer, entity=Channel())

    async def get_entity(self, target):
        from types import SimpleNamespace
        return SimpleNamespace()

    async def get_peer_id(self, channel):
        return self.chat_id

    async def send_file(self, target, path, caption=None, force_document=False):
        self.sent += 1
        from types import SimpleNamespace
        return SimpleNamespace(id=1000 + self.sent, chat_id=self.chat_id)

    async def get_messages(self, peer, ids):
        raise AssertionError("readback not used here")

    async def download_media(self, message, file):
        raise AssertionError("readback not used here")


class FakeRuntimeState:
    def __init__(self):
        self.connected = []
        self.clients = {}


def build_runtime(monkeypatch, tmp_path, env, *, bot=False):
    state = FakeRuntimeState()
    import telethon
    import telethon.sessions
    import telethon.tl.types

    class Channel:
        broadcast = True
        admin_rights = None
        creator = True

    monkeypatch.setattr(telethon.tl.types, "Channel", Channel)
    monkeypatch.setattr(telethon.sessions, "StringSession", lambda s: s)

    def fake_client(session, api_id, api_hash, **kwargs):
        label = kwargs.get("device_model", "")
        if label.startswith("TeleDrive Storage Client:"):
            label = label.split(":", 1)[1]
        client = FakeClient(label=label, runtime=state)
        state.clients[label] = client
        return client

    monkeypatch.setattr(telethon, "TelegramClient", fake_client)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    FakeClient.identity_bot = bot
    return state


@pytest.fixture
def runtime_env(tmp_path, monkeypatch):
    session_a = _write_session(tmp_path, "session-a.session")
    session_b = _write_session(tmp_path, "session-b.session")

    def apply(**overrides):
        env = {
            "TELEDRIVE_STORAGE_TRANSPORT": "telethon",
            "TELEDRIVE_STORAGE_MTPROTO_API_ID": "11111,22222",
            "TELEDRIVE_STORAGE_MTPROTO_API_HASH": "a" * 32 + "," + "b" * 32,
            "TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE": (
                str(session_a) + "," + str(session_b)
            ),
            "TELEDRIVE_STORAGE_MTPROTO_ACCOUNT": "user-a,user-b",
            "TELEDRIVE_STORAGE_MTPROTO_CHANNEL": "-10093744",
            "TELEDRIVE_STORAGE_MTPROTO_STRATEGY": "round_robin",
        }
        env.update(overrides)
        return build_runtime(monkeypatch, tmp_path, env)

    return apply


def _runtime(db, monkeypatch, tmp_path, **env):
    state = build_runtime(monkeypatch, tmp_path, {
        "TELEDRIVE_STORAGE_TRANSPORT": "telethon",
        "TELEDRIVE_STORAGE_MTPROTO_API_ID": "11111,22222",
        "TELEDRIVE_STORAGE_MTPROTO_API_HASH": "a" * 32 + "," + "b" * 32,
        "TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE": str(
            _write_session(tmp_path, "a.session")
        ) + "," + str(_write_session(tmp_path, "b.session")),
        "TELEDRIVE_STORAGE_MTPROTO_ACCOUNT": "user-a,user-b",
        "TELEDRIVE_STORAGE_MTPROTO_CHANNEL": "-10093744",
        **env,
    })
    runtime = UserSessionRuntime()
    return runtime, state


def test_runtime_starts_all_credential_sets_and_resolves_one_channel(
        monkeypatch, tmp_path):
    db = connect_database(str(tmp_path / "state.db"))
    try:
        runtime, state = _runtime(db, monkeypatch, tmp_path)
        asyncio.run(runtime.start())
        assert set(state.connected) == {"user-a", "user-b"}
        assert runtime.account == "user-a"
        assert runtime.chat_id == -10093744
        # Every set resolves the exact same dedicated channel.
        assert set(runtime.transports()) == {
            "user-a", "user-b",
        }
    finally:
        db.close()


def test_round_robin_rotates_sets_across_uploads(monkeypatch, tmp_path):
    db = connect_database(str(tmp_path / "state.db"))
    try:
        runtime, state = _runtime(db, monkeypatch, tmp_path)
        asyncio.run(runtime.start())
        journal = UserSendJournal(db)
        transport = UnifiedUserTransport(runtime, journal,
                                         min_upload_interval=0)
        source = tmp_path / "body.bin"
        source.write_bytes(b"payload-bytes")
        checksum = hashlib.sha256(b"payload-bytes").hexdigest()
        first = asyncio.run(transport.upload(
            source, object_id="obj-1", ordinal=0, checksum=checksum))
        second = asyncio.run(transport.upload(
            source, object_id="obj-1", ordinal=1, checksum=checksum))
        assert first[0] == "user-a"
        assert second[0] == "user-b"
        assert state.clients["user-a"].sent == 1
        assert state.clients["user-b"].sent == 1
        assert journal.unresolved_count() == 0
    finally:
        db.close()


def test_failover_switches_set_after_definite_flood_rejection(
        monkeypatch, tmp_path):
    import telethon.errors

    class Flood(telethon.errors.FloodWaitError):
        def __init__(self, seconds=3):
            super().__init__(request=None, capture=seconds)

    db = connect_database(str(tmp_path / "state.db"))
    try:
        runtime, state = _runtime(db, monkeypatch, tmp_path,
                                  TELEDRIVE_STORAGE_MTPROTO_STRATEGY="failover")
        asyncio.run(runtime.start())
        journal = UserSendJournal(db)
        transport = UnifiedUserTransport(runtime, journal,
                                         min_upload_interval=0)
        source = tmp_path / "body.bin"
        source.write_bytes(b"payload-bytes")
        checksum = hashlib.sha256(b"payload-bytes").hexdigest()

        async def flood_once(target, path, caption=None, force_document=False):
            if state.clients["user-a"].sent == 0:
                state.clients["user-a"].sent += 1
                raise Flood(2)
            state.clients["user-a"].sent += 1
            from types import SimpleNamespace
            return SimpleNamespace(id=999, chat_id=-10093744)

        state.clients["user-a"].send_file = flood_once
        result = asyncio.run(transport.upload(
            source, object_id="obj-1", ordinal=0, checksum=checksum))
        # The definite rejection cleared the set-A intent; set B performed
        # the retry and the receipt is journaled under the set-B label.
        assert result[0] == "user-b"
        assert journal.unresolved_count() == 0
        # A flood observation was recorded for the rejected set.
        from teledrive.storage.mtproto_rate_guard import MtprotoRateGuard
        assert MtprotoRateGuard(db).total_explicit_flood_observations() == 1
        assert MtprotoRateGuard(db).shared_cooldown_wait(
            "user-a", -10093744,
        ) == pytest.approx(2, abs=0.2)
        # Failover keeps the healthy set for subsequent uploads.
        second = asyncio.run(transport.upload(
            source, object_id="obj-1", ordinal=1, checksum=checksum))
        assert second[0] == "user-b"
    finally:
        db.close()


def test_ambiguous_send_outcome_never_triggers_cross_set_retry(
        monkeypatch, tmp_path):
    db = connect_database(str(tmp_path / "state.db"))
    try:
        runtime, state = _runtime(db, monkeypatch, tmp_path)
        asyncio.run(runtime.start())
        journal = UserSendJournal(db)
        transport = UnifiedUserTransport(runtime, journal,
                                         min_upload_interval=0)
        source = tmp_path / "body.bin"
        source.write_bytes(b"payload-bytes")
        checksum = hashlib.sha256(b"payload-bytes").hexdigest()

        async def ambiguous(target, path, caption=None, force_document=False):
            raise OSError("connection lost after send")

        state.clients["user-a"].send_file = ambiguous
        with pytest.raises(RuntimeError, match="uncertain"):
            asyncio.run(transport.upload(
                source, object_id="obj-1", ordinal=0, checksum=checksum))
        # The intent fence for set A is retained; set B was never tried.
        assert journal.unresolved_count() == 1
        assert state.clients["user-b"].sent == 0
    finally:
        db.close()


@pytest.mark.parametrize("overrides,match", [
    ({"TELEDRIVE_STORAGE_MTPROTO_API_ID": "11111,22222",
      "TELEDRIVE_STORAGE_MTPROTO_API_HASH": "a" * 32},
     "list"),  # one hash for two ids
    ({"TELEDRIVE_STORAGE_MTPROTO_ACCOUNT": "user-a"},
     "list"),
    ({"TELEDRIVE_STORAGE_MTPROTO_STRATEGY": "random"},
     "strategy"),
])
def test_malformed_credential_lists_fail_closed_before_network(
        monkeypatch, tmp_path, overrides, match):
    db = connect_database(str(tmp_path / "state.db"))
    try:
        runtime, state = _runtime(db, monkeypatch, tmp_path, **overrides)
        with pytest.raises(RuntimeError, match=match):
            asyncio.run(runtime.start())
    finally:
        db.close()


def test_bot_identity_in_any_credential_set_is_rejected(
        monkeypatch, tmp_path):
    db = connect_database(str(tmp_path / "state.db"))
    try:
        runtime, state = _runtime(db, monkeypatch, tmp_path)
        FakeClient.identity_bot = True
        with pytest.raises(RuntimeError, match="USER"):
            asyncio.run(runtime.start())
        FakeClient.identity_bot = False
    finally:
        db.close()
