"""Cross-worker SQL reservation and explicit flood-wait backoff isolation.

MTProto-only: the USER transport reserves shared account/channel send slots
in SQL and observes definitive FloodWaitError backoffs, so independent
workers never start parallel sends during a server cooldown.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from teledrive.db import connect_database
from teledrive.storage.mtproto_rate_guard import MtprotoRateGuard


def test_shared_mtproto_slots_order_workers_and_separate_accounts(tmp_path):
    db_path = str(tmp_path / "shared-slot.db")
    db1 = connect_database(db_path)
    db2 = connect_database(db_path)
    try:
        first, second = MtprotoRateGuard(db1), MtprotoRateGuard(db2)
        t = 5_000_000
        assert first.reserve_send_slot("user-a", -100111, 1.25, now_ms=t) == 0
        assert second.reserve_send_slot("user-a", -100111, 1.25, now_ms=t) == 1.25
        assert first.reserve_send_slot("user-a", -100111, 1.25, now_ms=t) == 2.5
        assert second.reserve_send_slot("user-b", -100111, 1.25, now_ms=t) == 0
        assert second.reserve_send_slot("user-a", -100112, 1.25, now_ms=t) == 0
        first.impose_shared_cooldown("user-a", -100111, 8, now_ms=t)
        assert second.reserve_send_slot("user-a", -100111, 1.25, now_ms=t) == 8
        # A shorter late flood cannot shorten the existing global cooldown.
        second.impose_shared_cooldown("user-a", -100111, 2, now_ms=t)
        assert first.reserve_send_slot("user-a", -100111, 1.25, now_ms=t) == 9.25
        # Skip a real delay: future timestamps take the next slot immediately.
        assert second.reserve_send_slot("user-a", -100111, 1.25,
                                        now_ms=t + 100_000) == 0
    finally:
        db2.close()
        db1.close()


def test_separate_os_processes_allocate_unique_global_send_slots(tmp_path):
    db_path = str(tmp_path / "multi-process-slot.db")
    db = connect_database(db_path)
    try:
        guard = MtprotoRateGuard(db)
        assert guard.reserve_send_slot("user-a", -100432, 0.75,
                                       now_ms=500000) == 0
    finally:
        db.close()
    child = r'''
import sys
from teledrive.db import connect_database
from teledrive.storage.mtproto_rate_guard import MtprotoRateGuard
db = connect_database(sys.argv[1])
try:
    guard = MtprotoRateGuard(db)
    wait = guard.reserve_send_slot("user-a", -100432, 0.75, now_ms=500000)
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
def test_invalid_shared_mtproto_send_throttle_never_allocates(
        tmp_path, account, chat, seconds):
    db = connect_database(str(tmp_path / "bad-slot.db"))
    try:
        guard = MtprotoRateGuard(db)
        with pytest.raises(ValueError):
            guard.reserve_send_slot(account, chat, seconds)
    finally:
        db.close()


def test_zero_min_interval_does_not_pace_but_respects_shared_flood(tmp_path):
    path = str(tmp_path / "no-throttle.db")
    db = connect_database(path)
    second = connect_database(path)
    try:
        guard = MtprotoRateGuard(db)
        other = MtprotoRateGuard(second)
        now = 70_000
        assert guard.reserve_send_slot("user", -10023, 0, now_ms=now) == 0
        assert other.reserve_send_slot("user", -10023, 0, now_ms=now) == 0
        # Zero pacing still needs a durable slot row for server flood backoff.
        guard.impose_shared_cooldown("user", -10023, 5, now_ms=now)
        assert other.reserve_send_slot("user", -10023, 0, now_ms=now) == 5
        assert other.shared_cooldown_wait("user", -10023, now_ms=now) == 5
        assert other.reserve_send_slot("other", -10023, 0, now_ms=now) == 0
        assert other.reserve_send_slot("user", -10024, 0, now_ms=now) == 0
        assert other.reserve_send_slot("user", -10023, 0, now_ms=now + 5_000) == 0
    finally:
        second.close()
        db.close()


def test_flood_after_other_workers_reserved_slots_remains_globally_visible(tmp_path):
    """A late Telegram flood-wait must fence PREVIOUSLY scheduled sends.

    Without a separate cooldown journal, sleeping workers have a stale
    reservation and may wake up/send in parallel during server backoff.
    """
    path = str(tmp_path / "late-flood.db")
    a = connect_database(path)
    b = connect_database(path)
    try:
        first, second = MtprotoRateGuard(a), MtprotoRateGuard(b)
        now = 8_000_000
        assert first.reserve_send_slot("shared", -10077, 1.25, now_ms=now) == 0
        assert second.reserve_send_slot("shared", -10077, 1.25, now_ms=now) == 1.25
        assert first.shared_cooldown_wait("shared", -10077, now_ms=now) == 0
        second.impose_shared_cooldown("shared", -10077, 8, now_ms=now)
        # Even the worker which reserved its slot BEFORE this flood must see it.
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


def test_late_shared_flood_is_rechecked_before_mtproto_send(tmp_path, monkeypatch):
    """A USER upload must inspect the new shared flood after SQL-slot sleeping."""
    import asyncio
    import hashlib
    import time
    from teledrive.storage.user_mode import UnifiedUserTransport, UserSendJournal

    class InspectOnlyUser:
        sends = 0

        async def upload(self, path, *, object_id, ordinal, checksum):
            self.sends += 1
            return "user", -100900, self.sends

    db = connect_database(str(tmp_path / "sleep-recheck.db"))
    try:
        journal = UserSendJournal(db)
        from teledrive.storage.mtproto_rate_guard import MtprotoRateGuard
        rate = MtprotoRateGuard(db)
        delegate = InspectOnlyUser()

        class FakeRuntime:
            account = "user"
            chat_id = -100900

            def require(self):
                return delegate

        transport = UnifiedUserTransport(FakeRuntime(), journal,
                                         rate=rate, min_upload_interval=0.001)
        source = tmp_path / "data.bin"
        source.write_bytes(b"known-safe-bytes")
        cooldown_checks = []
        original = rate.shared_cooldown_wait

        def delayed_check(account, chat_id, **kwargs):
            cooldown_checks.append((account, chat_id))
            # Simulate another OS worker receiving a flood-wait AFTER this
            # transport finished its old SQL send-slot wait.
            return 0.005 if len(cooldown_checks) == 1 else original(
                account, chat_id, **kwargs,
            )

        monkeypatch.setattr(rate, "shared_cooldown_wait", delayed_check)
        result = asyncio.run(transport.upload(
            source, object_id="late-cooldown", ordinal=0,
            checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
        ))
        assert result == ("user", -100900, 1)
        assert len(cooldown_checks) >= 2
        assert delegate.sends == 1
        assert journal and rate.total_explicit_flood_observations() == 0
    finally:
        db.close()


@pytest.mark.parametrize("value", [-1, 0, 1.0, True, 3601])
def test_invalid_shared_flood_cannot_change_existing_cooldown(tmp_path, value):
    db = connect_database(str(tmp_path / "flood-validation.db"))
    try:
        guard = MtprotoRateGuard(db)
        guard.reserve_send_slot("a", -100900, 1.0, now_ms=1000)
        guard.impose_shared_cooldown("a", -100900, 10, now_ms=1000)
        with pytest.raises(ValueError):
            guard.impose_shared_cooldown("a", -100900, value, now_ms=1000)
        assert guard.shared_cooldown_wait(
            "a", -100900, now_ms=1000,
        ) == 10
    finally:
        db.close()


def test_flood_sql_journal_fault_rolls_back_both_slot_and_cooldown(tmp_path):
    """A write failure cannot publish a partial flood-wait observation."""
    path = str(tmp_path / "flood-atomic.db")
    db = connect_database(path)
    try:
        guard = MtprotoRateGuard(db)
        t = 50_000
        assert guard.reserve_send_slot("user", -100905, 1.0, now_ms=t) == 0
        db.connection.exec_driver_sql(
            "CREATE TRIGGER reject_flood BEFORE INSERT ON "
            "storage_mtproto_chat_cooldowns "
            "BEGIN SELECT RAISE(ABORT, 'test unavailable cooldown SQL'); END"
        )
        db.commit()
        with pytest.raises(RuntimeError, match="cooldown"):
            guard.impose_shared_cooldown("user", -100905, 4, now_ms=t)
        assert guard.shared_cooldown_wait("user", -100905, now_ms=t) == 0
        assert guard.total_explicit_flood_observations() == 0
    finally:
        db.close()
