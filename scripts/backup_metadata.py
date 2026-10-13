#!/usr/bin/env python3
"""Consistent, fail-closed SQLite metadata snapshot for TeleDrive operators.

This is NOT a Telegram-media backup or a replacement for PostgreSQL/MySQL
native hot-backup tools. The output is a new restricted regular file, never an
in-place copy of the live database or an overwrite of an existing backup.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote


class BackupError(RuntimeError):
    pass


def backup_sqlite(database: Path, destination: Path) -> str:
    """Take an online SQLite backup and return its SHA-256 checksum.

    sqlite3.Connection.backup() obtains a transactionally consistent image.
    The destination is committed with an atomic same-directory hard link so
    pre-existing files and concurrent backup results are never overwritten.
    """
    source = Path(database).expanduser()
    target = Path(destination).expanduser()
    if source.is_symlink() or not source.is_file():
        raise BackupError("Source must be an existing regular database file")
    resolved = source.resolve(strict=True)
    if resolved == target.resolve():
        raise BackupError("Backup destination cannot be the live database")
    if not target.parent.is_dir():
        raise BackupError("Backup destination directory does not exist")
    if target.exists() or target.is_symlink():
        raise FileExistsError("Backup destination already exists")
    fd, filename = tempfile.mkstemp(prefix=".teledrive-snapshot-", dir=target.parent)
    temp = Path(filename)
    try:
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd = -1
        connection = sqlite3.connect(
            "file:" + quote(str(resolved), safe="/") + "?mode=ro",
            uri=True, timeout=30,
        )
        try:
            backup = sqlite3.connect(temp)
            try:
                connection.backup(backup, pages=256, sleep=0.1)
                check = backup.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise BackupError("SQLite snapshot integrity check failed")
                backup.commit()
            finally:
                backup.close()
        finally:
            connection.close()
        with temp.open("rb") as stream:
            digest = hashlib.sha256()
            while block := stream.read(1024 * 1024):
                digest.update(block)
        with temp.open("rb") as stream:
            os.fsync(stream.fileno())
        # Hard-link create is atomic and refuses existing destination, whereas
        # os.replace() could destroy a previously verified operator backup.
        os.link(temp, target, follow_symlinks=False)
        return digest.hexdigest()
    except sqlite3.DatabaseError as exc:
        raise BackupError("SQLite backup failed or source is corrupt") from exc
    finally:
        if fd != -1:
            os.close(fd)
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Consistent TeleDrive SQLite metadata snapshot")
    parser.add_argument("--database", required=True, type=Path,
                        help="Existing SQLite database file, not an SQL URL")
    parser.add_argument("--output", required=True, type=Path,
                        help="New backup file: never overwrites existing files")
    args = parser.parse_args()
    try:
        checksum = backup_sqlite(args.database, args.output)
    except (BackupError, OSError) as exc:
        parser.exit(1, "Backup refused: " + str(exc) + "\n")
    print("Backup complete. SHA-256: " + checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
