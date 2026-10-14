"""Cross-worker SQL reservation and explicit 429 backoff isolation."""
from __future__ import annotations

import subprocess
import sys

import pytest

from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotFileIndex


def test_shared_bot_slots_order_workers_and_separate_accounts(tmp_path):
    db_path = str(tmp_path / "shared-slot.db")
    db1 = connect_database(db_path)
    db2 = connect_database(db_path)
    try:
        first, second = BotFileIndex(db1), BotFileIndex(db2)
        t = 5_000_000
        assert first.reserve_send_slot("bot-a", -100111, 1.25, now_ms=t) == 0
        assert second.reserve_send_slot("bot-a", -100111, 1.25, now_ms=t) == 1.25
        assert first.reserve_send_slot("bot-a", -100111, 1.25, now_ms=t) == 2.5
        assert second.reserve_send_slot("bot-b", -100111, 1.25, now_ms=t) == 0
        assert second.reserve_send_slot("bot-a", -100112, 1.25, now_ms=t) == 0
        first.impose_shared_cooldown("bot-a", -100111, 8, now_ms=t)
        assert second.reserve_send_slot("bot-a", -100111, 1.25, now_ms=t) == 8
        # A shorter late 429 cannot shorten the existing global cooldown.
        second.impose_shared_cooldown("bot-a", -100111, 2, now_ms=t)
        assert first.reserve_send_slot("bot-a", -100111, 1.25, now_ms=t) == 9.25
        # Skip a real delay: future timestamps take the next slot immediately.
        assert second.reserve_send_slot("bot-a", -100111, 1.25,
                                        now_ms=t + 100_000) == 0
    finally:
        db2.close()
        db1.close()


def test_separate_os_processes_allocate_unique_global_send_slots(tmp_path):
    db_path = str(tmp_path / "multi-process-slot.db")
    db = connect_database(db_path)
    try:
        index = BotFileIndex(db)
        assert index.reserve_send_slot("bot-a", -100432, 0.75,
                                       now_ms=500000) == 0
    finally:
        db.close()
    child = r'''
import sys
from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotFileIndex
db = connect_database(sys.argv[1])
try:
    idx = BotFileIndex(db)
    wait = idx.reserve_send_slot("bot-a", -100432, 0.75, now_ms=500000)
    print(wait)
finally:
    db.close()
'''
    processes = [
        subprocess.Popen([sys.executable, "-c", child, db_path],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(4)
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr.decode(errors="replace")
        results.append(float(stdout.decode().strip()))
    assert sorted(results) == [0.75, 1.5, 2.25, 3.0]


@pytest.mark.parametrize("account,chat,seconds", [
    ("", -1001, 1), ("a", 0, 1), ("a", True, 1),
    ("a", -1001, -1), ("a", -1001, 61),
])
def test_invalid_shared_bot_send_throttle_never_allocates(tmp_path, account, chat, seconds):
    db = connect_database(str(tmp_path / "bad-slot.db"))
    try:
        index = BotFileIndex(db)
        with pytest.raises(ValueError):
            index.reserve_send_slot(account, chat, seconds)
    finally:
        db.close()


def test_zero_min_interval_does_not_pace_but_respects_shared_429(tmp_path):
    path = str(tmp_path / "no-throttle.db")
    db = connect_database(path)
    second = connect_database(path)
    try:
        index = BotFileIndex(db)
        other = BotFileIndex(second)
        now = 70_000
        assert index.reserve_send_slot("bot", -10023, 0, now_ms=now) == 0
        assert other.reserve_send_slot("bot", -10023, 0, now_ms=now) == 0
        # Zero pacing still needs a durable slot row for server 429 backoff.
        index.impose_shared_cooldown("bot", -10023, 5, now_ms=now)
        assert other.reserve_send_slot("bot", -10023, 0, now_ms=now) == 5
        assert other.shared_cooldown_wait("bot", -10023, now_ms=now) == 5
        assert other.reserve_send_slot("other", -10023, 0, now_ms=now) == 0
        assert other.reserve_send_slot("bot", -10024, 0, now_ms=now) == 0
        assert other.reserve_send_slot("bot", -10023, 0, now_ms=now + 5_000) == 0
    finally:
        second.close()
        db.close()

def test_429_after_other_workers_reserved_slots_remains_globally_visible(tmp_path):
    """A late Telegram rejection must fence PREVIOUSLY scheduled sends.

    Without a separate cooldown journal, sleeping workers have a stale
    reservation and may wake up/send in parallel during server backoff.
    """
    path = str(tmp_path / "late-429.db")
    a = connect_database(path)
    b = connect_database(path)
    try:
        first, second = BotFileIndex(a), BotFileIndex(b)
        now = 8_000_000
        assert first.reserve_send_slot("shared", -10077, 1.25, now_ms=now) == 0
        assert second.reserve_send_slot("shared", -10077, 1.25, now_ms=now) == 1.25
        assert first.shared_cooldown_wait("shared", -10077, now_ms=now) == 0
        second.impose_shared_cooldown("shared", -10077, 8, now_ms=now)
        # Even the worker which reserved its slot BEFORE this 429 must see it.
        assert first.shared_cooldown_wait("shared", -10077, now_ms=now) == 8
        assert first.shared_cooldown_wait(
            "shared", -10077, now_ms=now + 7_999,
        ) == 0.001
        assert first.shared_cooldown_wait(
            "shared", -10077, now_ms=now + 8_000,
        ) == 0
        assert first.shared_cooldown_wait("other", -10077, now_ms=now) == 0
        assert first.shared_cooldown_wait("shared", -10078, now_ms=now) == 0
        first.impose_shared_cooldown("shared", -10077, 2, now_ms=now)
        assert second.shared_cooldown_wait("shared", -10077, now_ms=now) == 8
        first.impose_shared_cooldown("shared", -10077, 12, now_ms=now)
        assert second.shared_cooldown_wait("shared", -10077, now_ms=now) == 12
    finally:
        b.close()
        a.close()


def test_late_shared_429_is_rechecked_before_bot_send(tmp_path, monkeypatch):
    """A Bot upload must inspect the new shared 429 after SQL-slot sleeping."""
    import asyncio
    import hashlib
    from teledrive.storage.bot_api_adapter import BotApiChunkTransport

    class InspectOnlyBot:
        sends = 0

        async def call(self, method, fields, source=None):
            assert method == "sendDocument"
            self.sends += 1
            return {
                "message_id": self.sends,
                "chat": {"id": -100900},
                "document": {"file_id": "test-telegram-file"},
            }

    db = connect_database(str(tmp_path / "sleep-recheck.db"))
    try:
        index = BotFileIndex(db)
        api = InspectOnlyBot()
        transport = BotApiChunkTransport(
            api, index, chat_id=-100900, min_upload_interval=0.001,
        )
        source = tmp_path / "data.bin"
        source.write_bytes(b"known-safe-bytes")
        cooldown_checks = []
        original = index.shared_cooldown_wait

        def delayed_check(account, chat_id):
            cooldown_checks.append((account, chat_id))
            # Simulate another OS worker receiving 429 AFTER this
            # transport finished its old SQL send-slot wait.
            return 0.005 if len(cooldown_checks) == 1 else original(
                account, chat_id,
            )

        monkeypatch.setattr(index, "shared_cooldown_wait", delayed_check)
        result = asyncio.run(transport.upload(
            source, object_id="late-cooldown", ordinal=0,
            checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
        ))
        assert result == ("bot", -100900, 1)
        assert len(cooldown_checks) >= 2
        assert api.sends == 1
        assert index.unresolved_count() == 0
    finally:
        db.close()


@pytest.mark.parametrize("value", [-1, 0, 1.0, True, 3601])
def test_invalid_shared_429_cannot_change_existing_cooldown(tmp_path, value):
    db = connect_database(str(tmp_path / "429-validation.db"))
    try:
        index = BotFileIndex(db)
        index.reserve_send_slot("a", -100900, 1.0, now_ms=1000)
        index.impose_shared_cooldown("a", -100900, 10, now_ms=1000)
        with pytest.raises(ValueError):
            index.impose_shared_cooldown("a", -100900, value, now_ms=1000)
        assert index.shared_cooldown_wait(
            "a", -100900, now_ms=1000,
        ) == 10
    finally:
        db.close()

def test_429_sql_journal_fault_rolls_back_both_slot_and_cooldown(tmp_path):
    """A write failure cannot publish a partial rate-limit observation."""
    path = str(tmp_path / "429-atomic.db")
    db = connect_database(path)
    try:
        index = BotFileIndex(db)
        t = 50_000
        assert index.reserve_send_slot("bot", -100905, 1.0, now_ms=t) == 0
        db.connection.exec_driver_sql(
            "CREATE TRIGGER reject_429 BEFORE INSERT ON storage_bot_chat_cooldowns "
            "BEGIN SELECT RAISE(ABORT, 'test unavailable cooldown SQL'); END"
        )
        db.commit()
        with pytest.raises(Exception, match="Shared Bot cooldown failed"):
            index.impose_shared_cooldown("bot", -100905, 15, now_ms=t)
        assert index.shared_cooldown_wait("bot", -100905, now_ms=t) == 0
        # If the send-slot update incorrectly committed ahead of cooldown,
        # the next slot would be pushed out 15 seconds instead of 1 second.
        assert index.reserve_send_slot("bot", -100905, 1.0, now_ms=t) == 1
        db.connection.exec_driver_sql("DROP TRIGGER reject_429")
        db.commit()
        index.impose_shared_cooldown("bot", -100905, 15, now_ms=t)
        assert index.shared_cooldown_wait("bot", -100905, now_ms=t) == 15
    finally:
        db.close()


def test_zero_interval_transport_rechecks_cross_worker_cooldown(
    tmp_path, monkeypatch,
):
    """Disabling local pacing must never disable a newer 429 server fence."""
    import asyncio
    import hashlib
    from teledrive.storage.bot_api_adapter import BotApiChunkTransport

    class OneSend:
        def __init__(self):
            self.sends = 0

        async def call(self, method, fields, source=None):
            assert method == "sendDocument"
            self.sends += 1
            return {
                "message_id": self.sends,
                "chat": {"id": -100990},
                "document": {"file_id": "zero-gap-file"},
            }

    database = connect_database(str(tmp_path / "zero-gap-recheck.db"))
    try:
        idx = BotFileIndex(database)
        api = OneSend()
        transport = BotApiChunkTransport(
            api, idx, chat_id=-100990, min_upload_interval=0,
        )
        original = idx.shared_cooldown_wait
        checks = []

        def late_429(account, chat_id):
            checks.append((account, chat_id))
            return 0.002 if len(checks) == 1 else original(account, chat_id)

        monkeypatch.setattr(idx, "shared_cooldown_wait", late_429)
        source = tmp_path / "safe.bin"
        source.write_bytes(b"safe zero-throttle Telegram payload")
        result = asyncio.run(transport.upload(
            source, object_id="zero-gap-recheck", ordinal=0,
            checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
        ))
        assert result == ("bot", -100990, 1)
        assert checks == [("bot", -100990), ("bot", -100990)]
        assert api.sends == 1
        assert idx.unresolved_count() == 0
    finally:
        database.close()


def test_zero_interval_actual_429_is_committed_across_sql_connections(tmp_path):
    """A definitive 61s rejection must survive a zero-throttle worker exit."""
    import asyncio
    import hashlib
    from teledrive.storage.bot_api_adapter import (
        BotApiChunkTransport, BotApiRateLimited,
    )

    class DefinitivelyRejected:
        sends = 0

        async def call(self, method, fields, source=None):
            assert method == "sendDocument"
            self.sends += 1
            raise BotApiRateLimited(61)

    path = str(tmp_path / "zero-gap-429.db")
    first = connect_database(path)
    second = connect_database(path)
    try:
        rejecting = DefinitivelyRejected()
        index = BotFileIndex(first)
        other = BotFileIndex(second)
        transport = BotApiChunkTransport(
            rejecting, index, chat_id=-100991, min_upload_interval=0,
        )
        source = tmp_path / "rejected.bin"
        source.write_bytes(b"realistic refused payload")
        with pytest.raises(BotApiRateLimited):
            asyncio.run(transport.upload(
                source, object_id="zero-gap-rejection", ordinal=0,
                checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
            ))
        assert rejecting.sends == 1
        assert index.unresolved_count() == 0
        assert other.shared_cooldown_wait("bot", -100991) > 59
        assert other.reserve_send_slot("bot", -100991, 0) > 59
        assert other.shared_cooldown_wait("bot", -100992) == 0
    finally:
        second.close()
        first.close()


def test_explicit_429_counter_is_atomic_and_survives_sql_restart(tmp_path):
    """A genuine Bot 429 observation survives independent SQL workers."""
    path = str(tmp_path / "durable-429-evidence.db")
    db1 = connect_database(path)
    db2 = connect_database(path)
    try:
        first, second = BotFileIndex(db1), BotFileIndex(db2)
        assert first.total_explicit_429_observations() == 0
        first.reserve_send_slot("a", -100501, 0, now_ms=1000)
        second.reserve_send_slot("a", -100502, 0, now_ms=1000)
        first.impose_shared_cooldown("a", -100501, 8, now_ms=1000)
        assert second.total_explicit_429_observations() == 1
        second.impose_shared_cooldown("a", -100502, 5, now_ms=2000)
        first.impose_shared_cooldown("a", -100501, 1, now_ms=3000)
        assert first.total_explicit_429_observations() == 3
    finally:
        db2.close()
        db1.close()
    reopened = connect_database(path)
    try:
        assert BotFileIndex(reopened).total_explicit_429_observations() == 3
    finally:
        reopened.close()


def test_explicit_429_counter_failure_rolls_back_cooldown_and_send_slot(tmp_path):
    """No success-looking 429 metric after a failed shared-SQL transaction."""
    path = str(tmp_path / "failed-metric.db")
    db = connect_database(path)
    try:
        index = BotFileIndex(db)
        index.reserve_send_slot("bot", -100905, 0, now_ms=1000)
        db.connection.exec_driver_sql(
            "CREATE TRIGGER reject_metric BEFORE INSERT "
            "ON storage_bot_429_observations "
            "BEGIN SELECT RAISE(ABORT, 'test failed rate evidence'); END"
        )
        db.commit()
        with pytest.raises(Exception, match="Shared Bot cooldown failed"):
            index.impose_shared_cooldown("bot", -100905, 15, now_ms=1000)
        assert index.total_explicit_429_observations() == 0
        assert index.shared_cooldown_wait("bot", -100905, now_ms=1000) == 0
        assert index.reserve_send_slot("bot", -100905, 0, now_ms=1000) == 0
        db.connection.exec_driver_sql("DROP TRIGGER reject_metric")
        db.commit()
        index.impose_shared_cooldown("bot", -100905, 15, now_ms=1000)
        assert index.total_explicit_429_observations() == 1
        assert index.shared_cooldown_wait("bot", -100905, now_ms=1000) == 15
    finally:
        db.close()


def test_fault_fixture_requires_prebooked_zero_interval_slot(tmp_path):
    """A synthetic definitive 429 must follow the real send-slot protocol.

    This contract caught the Docker DEV acceptance failure at fc255a8:
    pre-send intent alone is not a rate reservation and must not allow
    cooldown publication with a missing SQL row.
    """
    from teledrive.storage.bot_api_adapter import BotApiError

    db_path = str(tmp_path / "docker-fault-cooldown.db")
    first = connect_database(db_path)
    second = connect_database(db_path)
    try:
        sender = BotFileIndex(first)
        peer = BotFileIndex(second)
        with pytest.raises(BotApiError, match="shared cooldown row missing"):
            sender.impose_shared_cooldown(
                "fake-dev-telegram", -100700800900123, 7, now_ms=1000,
            )
        assert sender.total_explicit_429_observations() == 0

        # Even when normal send pacing is disabled, a real sender reserves
        # a durable shared slot BEFORE an explicit server 429 can occur.
        assert sender.reserve_send_slot(
            "fake-dev-telegram", -100700800900123, 0, now_ms=1000,
        ) == 0
        sender.impose_shared_cooldown(
            "fake-dev-telegram", -100700800900123, 7, now_ms=1000,
        )
        assert peer.total_explicit_429_observations() == 1
        assert peer.shared_cooldown_wait(
            "fake-dev-telegram", -100700800900123, now_ms=1000,
        ) == 7
    finally:
        second.close()
        first.close()
