#!/usr/bin/env python3
import argparse
import asyncio
from contextlib import contextmanager
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Union

import yaml
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError


@dataclass
class AppConfig:
    api_id: int
    api_hash: str
    phone: Optional[str]
    target: Union[str, int]
    session_path: str
    source_dir: str
    allowed_extensions: list[str]
    max_file_size_mb: int
    sleep_min_seconds: int
    sleep_max_seconds: int
    max_files_per_run: int
    max_files_per_day: int
    retry_attempts: int
    backoff_base_seconds: int
    floodwait_buffer_seconds: int
    caption_template: str
    send_mode: str
    db_path: str
    log_path: str
    log_level: str


def load_config(path: str) -> AppConfig:
    config_dir = Path(path).expanduser().resolve().parent

    def config_path(value: object) -> str:
        candidate = Path(str(value)).expanduser()
        return str(candidate if candidate.is_absolute() else config_dir / candidate)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    tg = raw["telegram"]
    up = raw["upload"]
    st = raw["state"]
    lg = raw["logging"]

    return AppConfig(
        api_id=int(tg["api_id"]),
        api_hash=str(tg["api_hash"]),
        phone=tg.get("phone"),
        target=tg["target"],
        session_path=config_path(tg["session_path"]),
        source_dir=config_path(up["source_dir"]),
        allowed_extensions=[str(x).lower() for x in up.get("allowed_extensions", [])],
        max_file_size_mb=int(up.get("max_file_size_mb", 0)),
        sleep_min_seconds=int(up["sleep_min_seconds"]),
        sleep_max_seconds=int(up["sleep_max_seconds"]),
        max_files_per_run=int(up["max_files_per_run"]),
        max_files_per_day=int(up["max_files_per_day"]),
        retry_attempts=int(up.get("retry_attempts", 5)),
        backoff_base_seconds=int(up.get("backoff_base_seconds", 10)),
        floodwait_buffer_seconds=int(up.get("floodwait_buffer_seconds", 5)),
        caption_template=str(up.get("caption_template", "{name}")),
        send_mode=str(up.get("send_mode", "document")).lower(),
        db_path=config_path(st["db_path"]),
        log_path=config_path(lg["log_path"]),
        log_level=str(lg.get("level", "INFO")),
    )


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def setup_logging(cfg: AppConfig) -> None:
    ensure_parent(cfg.log_path)
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(cfg.log_path, encoding="utf-8"), logging.StreamHandler()],
    )


def connect_db(db_path: str) -> sqlite3.Connection:
    ensure_parent(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            tg_message_id INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            first_seen_ts INTEGER NOT NULL,
            last_update_ts INTEGER NOT NULL,
            uploaded_ts INTEGER
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);")
    conn.commit()
    return conn


@contextmanager
def uploader_lock(db_path: str):
    """Prevent overlapping uploader processes using an exclusive lock file."""
    lock_path = Path(db_path).with_suffix(Path(db_path).suffix + ".lock")
    ensure_parent(str(lock_path))
    handle = None
    try:
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lock_path.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)
            except (OSError, ValueError):
                lock_path.unlink(missing_ok=True)
                handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise RuntimeError(f"Another uploader run is already active (pid {pid})")
        os.write(handle, str(os.getpid()).encode("ascii"))
        yield
    finally:
        if handle is not None:
            os.close(handle)
            lock_path.unlink(missing_ok=True)


def iter_source_files(source_dir: str) -> Iterable[str]:
    for root, _, files in os.walk(source_dir):
        for name in files:
            yield os.path.abspath(os.path.join(root, name))


def file_allowed(path: str, cfg: AppConfig) -> bool:
    if cfg.allowed_extensions:
        ext = os.path.splitext(path)[1].lower()
        if ext not in cfg.allowed_extensions:
            return False
    if cfg.max_file_size_mb > 0:
        max_bytes = cfg.max_file_size_mb * 1024 * 1024
        try:
            if os.path.getsize(path) > max_bytes:
                return False
        except OSError:
            return False
    return True


def scan_and_queue(conn: sqlite3.Connection, cfg: AppConfig) -> tuple[int, int, int]:
    now = int(time.time())
    inserted = 0
    updated = 0
    unchanged = 0

    for path in iter_source_files(cfg.source_dir):
        if not file_allowed(path, cfg):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        size = int(stat.st_size)
        mtime = float(stat.st_mtime)

        row = conn.execute("SELECT size, mtime, status FROM files WHERE path = ?", (path,)).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO files(path, size, mtime, status, first_seen_ts, last_update_ts)
                VALUES(?, ?, ?, 'pending', ?, ?)
                """,
                (path, size, mtime, now, now),
            )
            inserted += 1
            continue

        old_size, old_mtime, old_status = int(row[0]), float(row[1]), row[2]
        if old_size == size and abs(old_mtime - mtime) < 1e-6:
            unchanged += 1
            continue

        # File changed on disk; always re-queue safely.
        new_status = "pending"
        conn.execute(
            """
            UPDATE files
            SET size = ?, mtime = ?, status = ?, tg_message_id = NULL,
                uploaded_ts = NULL, last_error = NULL, last_update_ts = ?
            WHERE path = ?
            """,
            (size, mtime, new_status, now, path),
        )
        updated += 1

    conn.commit()
    return inserted, updated, unchanged


def uploaded_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM files
        WHERE status = 'uploaded'
          AND date(uploaded_ts, 'unixepoch', 'localtime') = date('now', 'localtime')
        """
    ).fetchone()
    return int(row[0] if row else 0)


def fetch_pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT path, size, mtime, attempts
        FROM files
        WHERE status IN ('pending', 'failed')
        ORDER BY first_seen_ts ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows


def mark_failed(conn: sqlite3.Connection, path: str, err: str, attempts_inc: int = 1) -> None:
    conn.execute(
        """
        UPDATE files
        SET status = 'failed',
            attempts = attempts + ?,
            last_error = ?,
            last_update_ts = ?
        WHERE path = ?
        """,
        (attempts_inc, err[:1000], int(time.time()), path),
    )
    conn.commit()


def mark_uploaded(conn: sqlite3.Connection, path: str, msg_id: Optional[int]) -> None:
    now = int(time.time())
    conn.execute(
        """
        UPDATE files
        SET status = 'uploaded',
            tg_message_id = ?,
            attempts = attempts + 1,
            last_error = NULL,
            uploaded_ts = ?,
            last_update_ts = ?
        WHERE path = ?
        """,
        (msg_id, now, now, path),
    )
    conn.commit()


def build_caption(path: str, template: str) -> str:
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    return template.format(name=name, stem=stem, ext=ext)[:1024]


def wait_for_auth_value(auth_dir: Optional[str], name: str, timeout: int = 600) -> str:
    if not auth_dir:
        return input(f"Please enter the {name}: ")
    print(f"Please enter the {name}:", flush=True)
    path = Path(auth_dir) / f"{name.replace(' ', '_')}.txt"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            value = ""
        if value:
            path.unlink(missing_ok=True)
            return value
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for Telegram {name}")


async def ensure_client(cfg: AppConfig, login_code: Optional[str] = None, login_password: Optional[str] = None, auth_dir: Optional[str] = None) -> TelegramClient:
    ensure_parent(cfg.session_path)
    client = TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash)
    if cfg.phone:
        await client.connect()
        if not await client.is_user_authorized():
            logging.info("Requesting a new Telegram login code for %s", cfg.phone)
            sent = await client.send_code_request(cfg.phone)
            logging.info("Telegram login code requested. Check your Telegram app for the code.")
            code = login_code or wait_for_auth_value(auth_dir, "code")
            try:
                await client.sign_in(cfg.phone, code=code, phone_code_hash=sent.phone_code_hash)
            except SessionPasswordNeededError:
                logging.info("Telegram 2-step verification is required.")
                password = login_password or wait_for_auth_value(auth_dir, "password")
                await client.sign_in(password=password)
        logging.info("Telegram authentication complete.")
    else:
        await client.start()
    return client


def resolve_target(target: Union[str, int]) -> Union[str, int]:
    if isinstance(target, int):
        return target
    if isinstance(target, str):
        s = target.strip()
        if s.startswith("-") and s[1:].isdigit():
            return int(s)
        if s.isdigit():
            return int(s)
        return s
    return str(target)


async def upload_one(client: TelegramClient, target, path: str, caption: str, send_mode: str):
    force_document = send_mode == "document"
    supports_streaming = send_mode == "media"
    return await client.send_file(
        entity=target,
        file=path,
        caption=caption,
        force_document=force_document,
        supports_streaming=supports_streaming,
    )


async def process_uploads(cfg: AppConfig, conn: sqlite3.Connection, login_code: Optional[str] = None, login_password: Optional[str] = None, auth_dir: Optional[str] = None) -> None:
    client = await ensure_client(cfg, login_code=login_code, login_password=login_password, auth_dir=auth_dir)
    async with client:
        target = await client.get_entity(resolve_target(cfg.target))
        today_count = uploaded_today(conn)
        if today_count >= cfg.max_files_per_day:
            logging.info(
                "Daily limit reached (%s/%s). Exiting.",
                today_count,
                cfg.max_files_per_day,
            )
            return

        pending = fetch_pending(conn, limit=cfg.max_files_per_run * 3)
        if not pending:
            logging.info("No pending files.")
            return

        done_this_run = 0
        for row in pending:
            if done_this_run >= cfg.max_files_per_run:
                break
            if today_count >= cfg.max_files_per_day:
                logging.info("Reached daily cap during run.")
                break

            path = row["path"]
            expected_size = int(row["size"])
            expected_mtime = float(row["mtime"])

            if not os.path.exists(path):
                mark_failed(conn, path, "File missing on disk")
                logging.warning("Missing file: %s", path)
                continue

            try:
                st = os.stat(path)
            except OSError as e:
                mark_failed(conn, path, f"os.stat failed: {e}")
                logging.warning("Stat failed for %s: %s", path, e)
                continue

            # Avoid uploading files that changed after scanning.
            if int(st.st_size) != expected_size or abs(float(st.st_mtime) - expected_mtime) > 1e-6:
                mark_failed(conn, path, "File changed since scan; will requeue on next scan")
                logging.warning("Changed file skipped (will requeue): %s", path)
                continue

            caption = build_caption(path, cfg.caption_template)
            ok = False
            for attempt in range(1, cfg.retry_attempts + 1):
                try:
                    msg = await upload_one(client, target, path, caption, cfg.send_mode)
                    msg_id = getattr(msg, "id", None)
                    mark_uploaded(conn, path, msg_id)
                    done_this_run += 1
                    today_count += 1
                    ok = True
                    logging.info(
                        "Uploaded (%s/%s today, %s this run): %s",
                        today_count,
                        cfg.max_files_per_day,
                        done_this_run,
                        path,
                    )
                    break
                except FloodWaitError as e:
                    wait_s = int(getattr(e, "seconds", 0)) + cfg.floodwait_buffer_seconds
                    mark_failed(conn, path, f"FloodWait: {wait_s}s", attempts_inc=0)
                    logging.warning("FloodWait for %ss while uploading %s", wait_s, path)
                    await asyncio.sleep(wait_s)
                    break
                except (RPCError, OSError, TimeoutError) as e:
                    if attempt >= cfg.retry_attempts:
                        mark_failed(conn, path, f"{type(e).__name__}: {e}")
                        logging.error("Permanent failure %s: %s", path, e)
                    else:
                        backoff = cfg.backoff_base_seconds * (2 ** (attempt - 1))
                        logging.warning(
                            "Retry %s/%s for %s after %ss due to: %s",
                            attempt,
                            cfg.retry_attempts,
                            path,
                            backoff,
                            e,
                        )
                        await asyncio.sleep(backoff)
                except Exception as e:
                    mark_failed(conn, path, f"Unexpected: {type(e).__name__}: {e}")
                    logging.exception("Unexpected error for %s: %s", path, e)
                    break

            if ok:
                sleep_s = random.randint(cfg.sleep_min_seconds, cfg.sleep_max_seconds)
                logging.info("Sleeping %ss", sleep_s)
                await asyncio.sleep(sleep_s)


def validate_config(cfg: AppConfig) -> None:
    if cfg.sleep_min_seconds <= 0 or cfg.sleep_max_seconds <= 0:
        raise ValueError("Sleep values must be > 0")
    if cfg.sleep_min_seconds > cfg.sleep_max_seconds:
        raise ValueError("sleep_min_seconds cannot be greater than sleep_max_seconds")
    if cfg.max_files_per_run <= 0:
        raise ValueError("max_files_per_run must be > 0")
    if cfg.max_files_per_day <= 0:
        raise ValueError("max_files_per_day must be > 0")
    if cfg.retry_attempts <= 0:
        raise ValueError("retry_attempts must be > 0")
    if cfg.backoff_base_seconds < 0 or cfg.floodwait_buffer_seconds < 0:
        raise ValueError("Backoff and floodwait buffer values cannot be negative")
    if cfg.max_file_size_mb < 0:
        raise ValueError("max_file_size_mb cannot be negative")
    if cfg.send_mode not in {"document", "media", "auto"}:
        raise ValueError("send_mode must be document, media, or auto")
    if not cfg.api_hash.strip():
        raise ValueError("api_hash must not be empty")
    if not str(cfg.target).strip():
        raise ValueError("target must not be empty")
    try:
        build_caption("example.txt", cfg.caption_template)
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError("caption_template may only use {name}, {stem}, and {ext}") from exc
    if not os.path.isdir(cfg.source_dir):
        raise ValueError(f"source_dir not found: {cfg.source_dir}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Safe Telegram auto uploader")
    p.add_argument("--config", required=True, help="Path to YAML config")
    p.add_argument("--scan-only", action="store_true", help="Only scan and queue files, then exit")
    p.add_argument("--run-once", action="store_true", help="Run one upload batch and exit")
    p.add_argument("--no-scan", action="store_true", help="Skip scan phase before upload")
    p.add_argument("--login-code", help=argparse.SUPPRESS)
    p.add_argument("--login-password", help=argparse.SUPPRESS)
    p.add_argument("--auth-dir", help=argparse.SUPPRESS)
    return p.parse_args()


async def async_main() -> int:
    args = parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg)
    validate_config(cfg)
    conn = connect_db(cfg.db_path)

    with uploader_lock(cfg.db_path):
        if not args.no_scan:
            inserted, updated, unchanged = scan_and_queue(conn, cfg)
            logging.info("Scan complete. inserted=%s updated=%s unchanged=%s", inserted, updated, unchanged)

        if args.scan_only and not args.run_once:
            return 0

        await process_uploads(cfg, conn, login_code=args.login_code, login_password=args.login_password, auth_dir=args.auth_dir)
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        logging.warning("Interrupted by user")
        return 130
    except Exception as e:
        logging.exception("Fatal error: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
