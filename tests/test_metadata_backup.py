"""Consistent SQLite backup protection, restart and corruption regression tests."""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from scripts.backup_metadata import BackupError, backup_sqlite


def test_online_snapshot_remains_consistent_after_source_changes(tmp_path):
    source = tmp_path / "metadata.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE journal(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        db.executemany("INSERT INTO journal(value) VALUES(?)", [(str(i),) for i in range(30)])
        db.commit()
    digest = backup_sqlite(source, destination)
    assert len(digest) == 64
    import hashlib
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO journal(value) VALUES('new data')")
        db.commit()
    with sqlite3.connect(destination) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 30
    with sqlite3.connect(source) as live:
        assert live.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 31
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_never_overwrite_or_replace_live_database(tmp_path):
    source = tmp_path / "metadata.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE example(id INTEGER)")
    target = tmp_path / "backup.db"
    target.write_bytes(b"pre-existing critical backup")
    with pytest.raises(FileExistsError):
        backup_sqlite(source, target)
    assert target.read_bytes() == b"pre-existing critical backup"
    with pytest.raises(BackupError, match="live database"):
        backup_sqlite(source, source)
    assert sqlite3.connect(source).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_corrupt_source_and_symlinks_fail_closed_and_clean_temporary_files(tmp_path):
    bad = tmp_path / "invalid.db"
    bad.write_bytes(b"not sqlite")
    output = tmp_path / "not-produced.db"
    with pytest.raises(BackupError):
        backup_sqlite(bad, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".teledrive-snapshot-*"))
    linked = tmp_path / "source-link.db"
    linked.symlink_to(bad)
    with pytest.raises(BackupError, match="regular"):
        backup_sqlite(linked, output)
    assert not output.exists()

def test_online_metadata_dr_backup_preserves_unknown_mtproto_send_fences(tmp_path):
    """A consistent backup must not turn an unknown Telegram send into a retry.

    The backup contains SQL-only metadata; no user media or Telegram messages
    are touched. A restored operator cannot silently skip the pre-send fence.
    """
    import hashlib

    from teledrive.db import connect_database
    from teledrive.storage.user_mode import UserSendJournal, mtproto_send_intents
    from sqlalchemy import select

    live_path = tmp_path / "mtproto-intents-live.db"
    archive_path = tmp_path / "mtproto-intents-backup.db"
    active = connect_database(str(live_path))
    try:
        journal = UserSendJournal(active)
        digest = hashlib.sha256(b"possibly-accepted").hexdigest()
        known = hashlib.sha256(b"received-a-valid-ack").hexdigest()
        journal.start("user-dr", -10012345, "unknown-channel-post", 0,
                      digest, len(b"possibly-accepted"))
        journal.start("user-dr", -10012345, "acknowledged-post", 0,
                      known, len(b"received-a-valid-ack"))
        journal.acknowledged("user-dr", -10012345, "acknowledged-post", 0, 17)
        assert journal.unresolved_count() == 1
        snapshot_sha = backup_sqlite(live_path, archive_path)
        assert len(snapshot_sha) == 64
    finally:
        active.close()

    recovered = connect_database(str(archive_path))
    try:
        journal = UserSendJournal(recovered)
        assert journal.unresolved_count() == 1
        # A restored operator cannot silently skip the pre-send fence: a
        # duplicate start for the unknown outcome stays rejected.
        with pytest.raises(RuntimeError, match="Unresolved MTProto upload intent"):
            journal.start("user-dr", -10012345, "unknown-channel-post", 0,
                          digest, len(b"possibly-accepted"))
        acknowledged = recovered.connection.execute(
            select(mtproto_send_intents.c.message_id).where(
                mtproto_send_intents.c.object_id == "acknowledged-post",
            )
        ).scalar_one()
        assert acknowledged == 17
        assert journal.unresolved_count() == 1
    finally:
        recovered.close()
