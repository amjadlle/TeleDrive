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


def test_zero_min_interval_does_not_allocate_or_sleep(tmp_path):
    db = connect_database(str(tmp_path / "no-throttle.db"))
    try:
        index = BotFileIndex(db)
        assert index.reserve_send_slot("bot", -10023, 0) == 0
        assert index.reserve_send_slot("bot", -10023, 0) == 0
    finally:
        db.close()
