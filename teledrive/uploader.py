#!/usr/bin/env python3
import argparse
import asyncio
from contextlib import contextmanager
import logging
import getpass
import os
import random
import re
import secrets
import shutil
import stat
import string
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

from .db import Database, DBRow, connect_database, normalize_database_url, redact_database_url, sqlite_database_path

import yaml
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError


# Telegram's free-account limit is 4,000 parts.  Telethon uses 512 KiB parts,
# so keep split files below the resulting ~2 GiB boundary with some headroom.
TELEGRAM_FREE_FILE_LIMIT = 1_900_000_000
SPLIT_BUFFER_SIZE = 1024 * 1024
ACCOUNT_STRATEGIES = {"single", "round_robin", "failover", "parallel"}
RUN_HOLDER = f"{os.getpid()}-{secrets.token_hex(12)}"
LEASE_NAME = "uploader-global"
LEASE_TTL_SECONDS = int(os.environ.get("TELEDRIVE_LEASE_TTL_SECONDS", "120"))
CLAIM_STALE_SECONDS = int(os.environ.get("TELEDRIVE_CLAIM_STALE_SECONDS", "86400"))
SPLIT_MIN_FREE_BYTES = int(os.environ.get("TELEDRIVE_SPLIT_MIN_FREE_BYTES", str(512 * 1024 * 1024)))


@dataclass(frozen=True)
class TelegramAccountConfig:
    name: str
    api_id: int
    api_hash: str
    phone: Optional[str]
    target: Union[str, int]
    session_path: str
    enabled: bool = True


@dataclass
class AppConfig:
    api_id: int
    api_hash: str
    phone: Optional[str]
    target: Union[str, int]
    session_path: str
    strategy: str
    accounts: list[TelegramAccountConfig]
    web: dict
    jobs: dict
    storage: dict
    state: dict
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


@dataclass
class AccountRuntime:
    config: TelegramAccountConfig
    client: TelegramClient
    target: object
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    floodwait_until: float = 0.0
    next_upload_at: float = 0.0


@dataclass
class AccountAttemptResult:
    success: bool
    message_id: Optional[int] = None
    retry_on_other_account: bool = False
    error: str = ""
    floodwait_seconds: int = 0


def _safe_account_token(name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()).strip("._-")
    return token or "account"


def load_config(path: str) -> AppConfig:
    config_dir = Path(path).expanduser().resolve().parent

    def config_path(value: object) -> str:
        candidate = Path(str(value)).expanduser()
        return str(candidate if candidate.is_absolute() else config_dir / candidate)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    tg = raw.get("telegram") or {}
    up = raw.get("upload") or {}
    st = raw.get("state") or {}
    lg = raw.get("logging") or {}
    web_section = raw.get("web") or {}
    jobs_section = raw.get("jobs") or {}
    storage_section = raw.get("storage") or {}

    strategy = str(tg.get("strategy", "single")).strip().lower()
    default_api_id = int(tg.get("api_id", 0) or 0)
    default_api_hash = str(tg.get("api_hash", ""))
    default_target: Union[str, int] = tg.get("target", "me")

    accounts: list[TelegramAccountConfig] = []
    raw_accounts = tg.get("accounts") or []
    if raw_accounts:
        if not isinstance(raw_accounts, list):
            raise ValueError("telegram.accounts must be a list")
        for index, item in enumerate(raw_accounts, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"telegram.accounts[{index - 1}] must be a mapping")
            name = str(item.get("name", f"account-{index}")).strip() or f"account-{index}"
            session_default = f"data/telethon_{_safe_account_token(name)}"
            accounts.append(TelegramAccountConfig(
                name=name,
                api_id=int(item.get("api_id", default_api_id) or 0),
                api_hash=str(item.get("api_hash", default_api_hash)),
                phone=item.get("phone"),
                target=item.get("target", default_target),
                session_path=config_path(item.get("session_path", session_default)),
                enabled=bool(item.get("enabled", True)),
            ))
    else:
        # Legacy behavior only when a telegram: section exists without an
        # explicit accounts key: one default account. An explicit empty
        # accounts list (or no telegram section at all) means no legacy
        # accounts — the unified service uses storage.apis instead.
        if "accounts" not in tg and tg:
            accounts.append(TelegramAccountConfig(
                name=str(tg.get("name", "default")).strip() or "default",
                api_id=default_api_id,
                api_hash=default_api_hash,
                phone=tg.get("phone"),
                target=default_target,
                session_path=config_path(
                    tg.get("session_path", "data/telethon_user")),
                enabled=True,
            ))

    enabled_accounts = [a for a in accounts if a.enabled]
    first = enabled_accounts[0] if enabled_accounts else (
        accounts[0] if accounts else TelegramAccountConfig(
            name="default", api_id=default_api_id, api_hash=default_api_hash,
            phone=None, target=default_target,
            session_path=config_path("data/telethon_user"), enabled=True,
        )
    )
    return AppConfig(
        api_id=first.api_id,
        api_hash=first.api_hash,
        phone=first.phone,
        target=first.target,
        session_path=first.session_path,
        strategy=strategy,
        accounts=accounts,
        source_dir=config_path(up.get("source_dir", "/uploads")),
        allowed_extensions=[str(x).lower() for x in up.get("allowed_extensions", [])],
        max_file_size_mb=int(up.get("max_file_size_mb", 0)),
        sleep_min_seconds=int(up.get("sleep_min_seconds", 60)),
        sleep_max_seconds=int(up.get("sleep_max_seconds", 90)),
        max_files_per_run=int(up.get("max_files_per_run", 12)),
        max_files_per_day=int(up.get("max_files_per_day", 100)),
        retry_attempts=int(up.get("retry_attempts", 5)),
        backoff_base_seconds=int(up.get("backoff_base_seconds", 10)),
        floodwait_buffer_seconds=int(up.get("floodwait_buffer_seconds", 5)),
        caption_template=str(up.get("caption_template", "{name}")),
        send_mode=str(up.get("send_mode", "document")).lower(),
        db_path=normalize_database_url(
            st.get("database_url") or st.get("db_path", "data/state.db"),
            config_dir=config_dir,
        ),
        log_path=config_path(lg.get("log_path", "data/uploader.log")),
        log_level=str(lg.get("level", "INFO")),
        web=web_section,
        jobs=jobs_section,
        storage=storage_section,
        state=st,
    )


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


_LOG_SECRET_URL_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^:/@\s]+:)[^@\s]+(@)")
_LOG_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:password|passwd|pwd|token|access_token|secret|api_key|apikey)=)[^&#\s]+"
)


def _scrub_log_text(value: str) -> str:
    value = _LOG_SECRET_URL_RE.sub(r"\1***\2", value)
    return _LOG_QUERY_SECRET_RE.sub(r"\1***", value)


def _safe_error_text(value: object, limit: int = 1000) -> str:
    cleaned = _scrub_log_text(str(value)).replace("\r", "\\r").replace("\n", "\\n")
    return cleaned[:limit]


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _scrub_log_text(super().format(record))


def setup_logging(cfg: AppConfig) -> None:
    ensure_parent(cfg.log_path)
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    handlers = [
        logging.FileHandler(cfg.log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(level=level, handlers=handlers)
    formatter = _RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s")
    for handler in handlers:
        handler.setFormatter(formatter)


def connect_db(db_path: str) -> Database:
    return connect_database(db_path)


def mark_account_state(
    conn: Database,
    name: str,
    state: str,
    *,
    last_error: Optional[str] = None,
    cooldown_until: Optional[int] = None,
    last_success: bool = False,
) -> None:
    now = int(time.time())
    existing = conn.account_state(name)
    success_ts = now if last_success else (existing["last_success_ts"] if existing else None)
    conn.set_account_state(
        name, state,
        last_error=_safe_error_text(last_error) if last_error else None,
        cooldown_until=cooldown_until,
        last_success_ts=success_ts,
        now=now,
    )
    conn.commit()


@contextmanager
def uploader_lock(db_path: str):
    """Prevent overlapping uploader processes using an exclusive lock file."""
    if "://" in db_path:
        import hashlib
        token = hashlib.sha256(redact_database_url(db_path).encode("utf-8")).hexdigest()[:20]
        lock_root = Path(os.environ.get("TELEDRIVE_LOCK_DIR", tempfile.gettempdir())).expanduser()
        lock_root.mkdir(parents=True, exist_ok=True)
        try:
            lock_root.chmod(0o700)
        except OSError:
            pass
        lock_path = lock_root / f"teledrive-{token}.lock"
    else:
        lock_path = Path(db_path).with_suffix(Path(db_path).suffix + ".lock")
    ensure_parent(str(lock_path))
    handle = None
    try:
        try:
            handle = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            try:
                if lock_path.is_symlink():
                    lock_path.unlink(missing_ok=True)
                    handle = os.open(
                        str(lock_path),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    os.write(handle, str(os.getpid()).encode("ascii"))
                    yield
                    return
                pid = int(lock_path.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)
            except (OSError, ValueError):
                lock_path.unlink(missing_ok=True)
                handle = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            else:
                raise RuntimeError(f"Another uploader run is already active (pid {pid})")
        os.write(handle, str(os.getpid()).encode("ascii"))
        yield
    finally:
        if handle is not None:
            os.close(handle)
            lock_path.unlink(missing_ok=True)


def _path_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def _validate_allowed_roots(path: str, env_name: str) -> None:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return
    roots = [item.strip() for item in raw.split(os.pathsep) if item.strip()]
    if not roots or not any(_path_within(path, root) for root in roots):
        raise ValueError(f"{path} is outside allowed roots configured by {env_name}")


def iter_source_files(source_dir: str) -> Iterable[str]:
    source_real = os.path.realpath(source_dir)
    for root, dirs, files in os.walk(source_real, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            path = os.path.abspath(os.path.join(root, name))
            if os.path.islink(path):
                logging.warning("Skipping symlink: %r", path)
                continue
            if not _path_within(path, source_real):
                logging.warning("Skipping path outside source root: %r", path)
                continue
            try:
                file_stat = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                logging.warning("Skipping non-regular file: %r", path)
                continue
            yield path


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


def scan_and_queue(
    conn: Database,
    cfg: AppConfig,
    lease_heartbeat: Optional[Callable[[], None]] = None,
) -> tuple[int, int, int]:
    now = int(time.time())
    inserted = updated = unchanged = 0
    last_heartbeat = time.monotonic()
    for path in iter_source_files(cfg.source_dir):
        if lease_heartbeat and time.monotonic() - last_heartbeat >= max(5, LEASE_TTL_SECONDS // 4):
            lease_heartbeat()
            last_heartbeat = time.monotonic()
        if not file_allowed(path, cfg):
            continue
        try:
            file_stat = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(file_stat.st_mode):
            logging.warning("Skipping non-regular file: %r", path)
            continue
        size, mtime = int(file_stat.st_size), float(file_stat.st_mtime)
        row = conn.file_signature(path)
        if row is None:
            conn.insert_pending(path, size, mtime, now)
            inserted += 1
            continue
        if int(row[0]) == size and abs(float(row[1]) - mtime) < 1e-6:
            unchanged += 1
            continue
        conn.requeue_changed(path, size, mtime, now)
        updated += 1
    conn.commit()
    return inserted, updated, unchanged


def uploaded_today(conn: Database) -> int:
    now = time.time()
    local = time.localtime(now)
    start = int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, local.tm_wday, local.tm_yday, local.tm_isdst)))
    return conn.uploaded_between(start, start + 86400)


def fetch_pending(conn: Database, limit: int) -> list[DBRow]:
    return conn.pending(limit, int(time.time()), CLAIM_STALE_SECONDS)


def mark_failed(conn: Database, path: str, err: str, attempts_inc: int = 1) -> None:
    conn.mark_failed(path, _safe_error_text(err), attempts_inc, int(time.time()))
    conn.commit()


def mark_uploaded(conn: Database, path: str, msg_id: Optional[int], account_name: str) -> None:
    conn.mark_uploaded(path, msg_id, account_name, int(time.time()))
    conn.commit()


def build_caption(path: str, template: str) -> str:
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    allowed = {"name", "stem", "ext"}
    formatter = string.Formatter()
    for _, field_name, format_spec, conversion in formatter.parse(template):
        if field_name is None:
            continue
        if field_name not in allowed or format_spec or conversion:
            raise ValueError("caption_template may only use plain {name}, {stem}, and {ext} placeholders")
    return template.format(name=name, stem=stem, ext=ext)[:1024]


def wait_for_auth_value(auth_dir: Optional[str], name: str, timeout: int = 600, account_name: Optional[str] = None) -> str:
    suffix = f" for account '{account_name}'" if account_name else ""
    if not auth_dir:
        if name == "password":
            return getpass.getpass(f"Please enter the {name}{suffix}: ")
        return input(f"Please enter the {name}{suffix}: ")
    print(f"Please enter the {name}{suffix}:", flush=True)
    root = Path(auth_dir)
    generic = root / f"{name.replace(' ', '_')}.txt"
    if account_name:
        candidates = [root / f"{_safe_account_token(account_name)}_{name.replace(' ', '_')}.txt"]
    else:
        candidates = [generic]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for path in candidates:
            try:
                if path.is_symlink():
                    path.unlink(missing_ok=True)
                    continue
                value = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                value = ""
            if value:
                path.unlink(missing_ok=True)
                return value
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for Telegram {name}{suffix}")


async def ensure_client(account: TelegramAccountConfig, login_code: Optional[str] = None, login_password: Optional[str] = None, auth_dir: Optional[str] = None) -> TelegramClient:
    ensure_parent(account.session_path)
    client = TelegramClient(account.session_path, account.api_id, account.api_hash)
    if account.phone:
        await client.connect()
        if not await client.is_user_authorized():
            logging.info("[%s] Requesting Telegram login code", account.name)
            sent = await client.send_code_request(account.phone)
            code = login_code or wait_for_auth_value(auth_dir, "code", account_name=account.name)
            try:
                await client.sign_in(account.phone, code=code, phone_code_hash=sent.phone_code_hash)
            except SessionPasswordNeededError:
                password = login_password or wait_for_auth_value(auth_dir, "password", account_name=account.name)
                await client.sign_in(password=password)
        logging.info("[%s] Telegram authentication complete.", account.name)
    else:
        await client.connect()
        if not await client.is_user_authorized():
            if auth_dir:
                raise RuntimeError(
                    f"Telegram account '{account.name}' has no authorized session; configure phone for Web UI login"
                )
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
        parse_mode=None,
    )


def split_for_telegram(
    path: str,
    max_bytes: int = TELEGRAM_FREE_FILE_LIMIT,
    heartbeat: Optional[Callable[[], None]] = None,
) -> tuple[str, list[str]]:
    """Create raw, lossless parts for a file that exceeds Telegram's free limit."""
    source = Path(path)
    total_size = source.stat().st_size
    part_count = (total_size + max_bytes - 1) // max_bytes
    split_root = Path(os.environ.get("TELEDRIVE_SPLIT_DIR", tempfile.gettempdir())).expanduser()
    split_root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(split_root).free
    required_bytes = total_size + SPLIT_MIN_FREE_BYTES
    if free_bytes < required_bytes:
        raise OSError(
            f"Insufficient split space: need {required_bytes} bytes including reserve, have {free_bytes}"
        )
    temp_dir = tempfile.mkdtemp(prefix="teledrive-parts-", dir=str(split_root))
    width = max(2, len(str(part_count)))
    part_paths: list[str] = []

    try:
        with source.open("rb") as source_file:
            last_heartbeat = time.monotonic()
            for part_number in range(1, part_count + 1):
                part_path = Path(temp_dir) / f"{source.stem}.part{part_number:0{width}d}{source.suffix}"
                remaining = min(max_bytes, total_size - (part_number - 1) * max_bytes)
                with part_path.open("wb") as part_file:
                    while remaining:
                        block = source_file.read(min(SPLIT_BUFFER_SIZE, remaining))
                        if not block:
                            raise OSError(f"Unexpected end of file while splitting {path}")
                        part_file.write(block)
                        remaining -= len(block)
                        if heartbeat and time.monotonic() - last_heartbeat >= max(5, LEASE_TTL_SECONDS // 4):
                            heartbeat()
                            last_heartbeat = time.monotonic()
                part_paths.append(str(part_path))
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    logging.info("Split oversized file into %s parts: %r", len(part_paths), path)
    return temp_dir, part_paths


def is_file_parts_invalid(error: BaseException) -> bool:
    message = str(error).lower()
    return "file_parts_invalid" in message or "number of file parts is invalid" in message


async def _wait_for_account_pacing(cfg: AppConfig, runtime: AccountRuntime) -> None:
    delay = runtime.next_upload_at - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)


async def _attempt_upload_on_account(cfg: AppConfig, runtime: AccountRuntime, source_path: str, caption: str, upload_paths: list[str]) -> AccountAttemptResult:
    async with runtime.lock:
        now = time.monotonic()
        if runtime.floodwait_until > now:
            remaining = max(1, int(runtime.floodwait_until - now))
            return AccountAttemptResult(False, retry_on_other_account=True, error=f"Account cooldown for {remaining}s", floodwait_seconds=remaining)
        for attempt in range(1, cfg.retry_attempts + 1):
            sent_ids: list[int] = []
            try:
                await _wait_for_account_pacing(cfg, runtime)
                msg_id: Optional[int] = None
                for part_number, upload_path in enumerate(upload_paths, start=1):
                    part_caption, send_mode = caption, cfg.send_mode
                    if len(upload_paths) > 1:
                        part_caption = f"{caption} [part {part_number}/{len(upload_paths)}]"
                        send_mode = "document"
                    msg = await upload_one(runtime.client, runtime.target, upload_path, part_caption, send_mode)
                    msg_id = getattr(msg, "id", None)
                    if msg_id is not None:
                        sent_ids.append(int(msg_id))
                if msg_id is None:
                    raise RuntimeError(f"Telegram returned no message ID for {source_path}")
                runtime.next_upload_at = time.monotonic() + random.randint(cfg.sleep_min_seconds, cfg.sleep_max_seconds)
                return AccountAttemptResult(True, message_id=msg_id)
            except FloodWaitError as exc:
                wait_s = int(getattr(exc, "seconds", 0)) + cfg.floodwait_buffer_seconds
                runtime.floodwait_until = max(runtime.floodwait_until, time.monotonic() + wait_s)
                if sent_ids:
                    try: await runtime.client.delete_messages(runtime.target, sent_ids)
                    except Exception: pass
                return AccountAttemptResult(False, retry_on_other_account=True, error=f"FloodWait: {wait_s}s", floodwait_seconds=wait_s)
            except (RPCError, OSError, TimeoutError) as exc:
                if sent_ids:
                    try: await runtime.client.delete_messages(runtime.target, sent_ids)
                    except Exception: pass
                if is_file_parts_invalid(exc):
                    return AccountAttemptResult(False, error=f"Too large for Telegram free-account limit: {exc}")
                if attempt >= cfg.retry_attempts:
                    return AccountAttemptResult(False, retry_on_other_account=True, error=f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(cfg.backoff_base_seconds * (2 ** (attempt - 1)))
            except Exception as exc:
                if sent_ids:
                    try: await runtime.client.delete_messages(runtime.target, sent_ids)
                    except Exception: pass
                return AccountAttemptResult(False, error=f"Unexpected: {type(exc).__name__}: {exc}")
    return AccountAttemptResult(False, error="Unknown upload failure")


def _validate_pending_file(row: DBRow, conn: Database, cfg: AppConfig) -> Optional[str]:
    path = row["path"]
    if os.path.islink(path) or not _path_within(path, cfg.source_dir):
        mark_failed(conn, path, "Unsafe path: symlink or outside source root")
        return "Unsafe path"
    if not os.path.exists(path):
        mark_failed(conn, path, "File missing on disk"); return "File missing on disk"
    try: file_stat = os.lstat(path)
    except OSError as exc:
        mark_failed(conn, path, f"os.lstat failed: {exc}"); return f"os.lstat failed: {exc}"
    if not stat.S_ISREG(file_stat.st_mode):
        mark_failed(conn, path, "Unsafe path: not a regular file")
        return "Unsafe path"
    if int(file_stat.st_size) != int(row["size"]) or abs(float(file_stat.st_mtime) - float(row["mtime"])) > 1e-6:
        mark_failed(conn, path, "File changed since scan; will requeue on next scan"); return "File changed since scan"
    return None


async def _process_row(cfg: AppConfig, conn: Database, row: DBRow, account_order: list[AccountRuntime]) -> bool:
    path = row["path"]
    claim_token = f"{RUN_HOLDER}:{secrets.token_hex(8)}"
    if not conn.claim_file(path, claim_token, int(time.time()), CLAIM_STALE_SECONDS):
        logging.info("Skipping already-claimed file: %r", path)
        return False
    error = _validate_pending_file(row, conn, cfg)
    if error:
        logging.warning("Skipped %r: %s", path, error); return False
    caption = build_caption(path, cfg.caption_template)
    split_dir: Optional[str] = None
    upload_paths = [path]
    try:
        if int(row["size"]) >= TELEGRAM_FREE_FILE_LIMIT:
            split_dir, upload_paths = split_for_telegram(
                path,
                heartbeat=lambda: _renew_lease_now(conn),
            )
        last_error = "No available Telegram account"
        for runtime in account_order:
            mark_account_state(conn, runtime.config.name, "uploading")
            result = await _attempt_upload_on_account(cfg, runtime, path, caption, upload_paths)
            if result.success:
                mark_uploaded(conn, path, result.message_id, runtime.config.name)
                mark_account_state(conn, runtime.config.name, "ready", last_success=True)
                logging.info("[%s] Uploaded: %r", runtime.config.name, path)
                return True
            last_error = result.error or last_error
            if result.floodwait_seconds:
                mark_account_state(conn, runtime.config.name, "cooldown", last_error=last_error, cooldown_until=int(time.time()) + result.floodwait_seconds)
            else:
                mark_account_state(conn, runtime.config.name, "error", last_error=last_error)
            if not result.retry_on_other_account:
                break
        mark_failed(conn, path, last_error)
        logging.error("Upload failed for %r: %s", path, last_error)
        return False
    except Exception as exc:
        mark_failed(conn, path, f"{type(exc).__name__}: {exc}")
        logging.exception("Unexpected upload pipeline failure for %r", path)
        return False
    finally:
        if split_dir: shutil.rmtree(split_dir, ignore_errors=True)


def _rotated_accounts(accounts: list[AccountRuntime], start: int) -> list[AccountRuntime]:
    if not accounts: return []
    index = start % len(accounts)
    return accounts[index:] + accounts[:index]


async def _connect_accounts(cfg: AppConfig, conn: Database, login_code: Optional[str], login_password: Optional[str], auth_dir: Optional[str]) -> list[AccountRuntime]:
    enabled = [a for a in cfg.accounts if a.enabled]
    requested = enabled[:1] if cfg.strategy == "single" else enabled
    runtimes: list[AccountRuntime] = []
    one_shot_code, one_shot_password = login_code, login_password
    for account in requested:
        client: Optional[TelegramClient] = None
        try:
            mark_account_state(conn, account.name, "connecting")
            client = await ensure_client(account, one_shot_code, one_shot_password, auth_dir)
            one_shot_code = one_shot_password = None
            target = await client.get_entity(resolve_target(account.target))
            runtimes.append(AccountRuntime(account, client, target))
            mark_account_state(conn, account.name, "ready")
        except Exception as exc:
            mark_account_state(conn, account.name, "unavailable", last_error=f"{type(exc).__name__}: {exc}")
            if client is not None:
                try: await client.disconnect()
                except Exception: pass
            if cfg.strategy == "single": raise
    if not runtimes: raise RuntimeError("No enabled Telegram account could be connected")
    return runtimes


async def _disconnect_accounts(conn: Database, runtimes: list[AccountRuntime]) -> None:
    for runtime in runtimes:
        try:
            await runtime.client.disconnect()
            remaining = max(0, int(runtime.floodwait_until - time.monotonic() + 0.999))
            if remaining:
                mark_account_state(conn, runtime.config.name, "cooldown", last_error=f"FloodWait cooldown: {remaining}s remaining", cooldown_until=int(time.time()) + remaining)
            else:
                mark_account_state(conn, runtime.config.name, "idle")
        except Exception as exc:
            mark_account_state(conn, runtime.config.name, "error", last_error=f"Disconnect: {exc}")


async def process_uploads(
    cfg: AppConfig,
    conn: Database,
    login_code: Optional[str] = None,
    login_password: Optional[str] = None,
    auth_dir: Optional[str] = None,
    lease_lost: Optional[asyncio.Event] = None,
) -> None:
    runtimes = await _connect_accounts(cfg, conn, login_code, login_password, auth_dir)
    try:
        remaining_today = cfg.max_files_per_day - uploaded_today(conn)
        if remaining_today <= 0:
            logging.info("Daily limit reached."); return
        batch_limit = min(cfg.max_files_per_run, remaining_today)
        rows = fetch_pending(conn, max(batch_limit * 3, batch_limit))[:batch_limit]
        if not rows:
            logging.info("No pending files."); return
        if cfg.strategy == "parallel" and len(runtimes) > 1:
            buckets: list[list[DBRow]] = [[] for _ in runtimes]
            for index, row in enumerate(rows): buckets[index % len(runtimes)].append(row)
            async def worker(start_index: int, bucket: list[DBRow]) -> int:
                count, order = 0, _rotated_accounts(runtimes, start_index)
                for row in bucket:
                    if lease_lost is not None and lease_lost.is_set():
                        raise RuntimeError("Distributed uploader lease lost")
                    if await _process_row(cfg, conn, row, order): count += 1
                return count
            done = sum(await asyncio.gather(*(worker(i,b) for i,b in enumerate(buckets) if b)))
        else:
            done, rr_cursor = 0, 0
            for row in rows:
                if lease_lost is not None and lease_lost.is_set():
                    raise RuntimeError("Distributed uploader lease lost")
                if cfg.strategy == "round_robin":
                    order = _rotated_accounts(runtimes, rr_cursor); rr_cursor = (rr_cursor + 1) % len(runtimes)
                elif cfg.strategy == "failover": order = list(runtimes)
                else: order = [runtimes[0]]
                if await _process_row(cfg, conn, row, order): done += 1
        logging.info("Upload batch complete. uploaded=%s failed_or_deferred=%s strategy=%s", done, len(rows)-done, cfg.strategy)
    finally:
        await _disconnect_accounts(conn, runtimes)


def validate_config(cfg: AppConfig) -> None:
    if cfg.strategy not in ACCOUNT_STRATEGIES:
        raise ValueError("telegram.strategy must be single, round_robin, failover, or parallel")
    if not cfg.accounts or not any(a.enabled for a in cfg.accounts):
        raise ValueError("At least one Telegram account must be enabled")
    names, sessions = set(), set()
    for account in cfg.accounts:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", account.name):
            raise ValueError("Telegram account name must be 1-64 characters using only letters, digits, '.', '_' or '-'")
        if account.name in names: raise ValueError(f"Duplicate Telegram account name: {account.name}")
        names.add(account.name)
        session = os.path.abspath(account.session_path)
        if session in sessions: raise ValueError(f"Telegram accounts must use different session_path values: {account.session_path}")
        sessions.add(session)
        if account.enabled:
            if account.api_id <= 0: raise ValueError(f"Telegram account '{account.name}' has an invalid api_id")
            if not account.api_hash.strip(): raise ValueError(f"Telegram account '{account.name}' api_hash must not be empty")
            if account.phone and not re.fullmatch(r"\+?[0-9]{5,20}", str(account.phone)):
                raise ValueError(f"Telegram account '{account.name}' has an invalid phone number")
            target_text = str(account.target)
            if not target_text.strip(): raise ValueError(f"Telegram account '{account.name}' target must not be empty")
            if len(target_text) > 256 or any(ord(ch) < 32 for ch in target_text):
                raise ValueError(f"Telegram account '{account.name}' target contains invalid characters")
    if cfg.sleep_min_seconds <= 0 or cfg.sleep_max_seconds <= 0: raise ValueError("Sleep values must be > 0")
    if cfg.sleep_min_seconds > cfg.sleep_max_seconds: raise ValueError("sleep_min_seconds cannot be greater than sleep_max_seconds")
    if cfg.max_files_per_run <= 0 or cfg.max_files_per_day <= 0: raise ValueError("Upload limits must be > 0")
    if cfg.retry_attempts <= 0: raise ValueError("retry_attempts must be > 0")
    if cfg.backoff_base_seconds < 0 or cfg.floodwait_buffer_seconds < 0: raise ValueError("Backoff and floodwait buffer values cannot be negative")
    if cfg.max_file_size_mb < 0: raise ValueError("max_file_size_mb cannot be negative")
    if cfg.send_mode not in {"document", "media", "auto"}: raise ValueError("send_mode must be document, media, or auto")
    try: build_caption("example.txt", cfg.caption_template)
    except (KeyError, ValueError, IndexError) as exc: raise ValueError("caption_template may only use {name}, {stem}, and {ext}") from exc
    validate_storage_paths(cfg)


def validate_storage_paths(cfg: AppConfig) -> None:
    if not os.path.isdir(cfg.source_dir):
        raise ValueError(f"source_dir not found: {cfg.source_dir}")
    _validate_allowed_roots(cfg.source_dir, "TELEDRIVE_ALLOWED_SOURCE_ROOTS")
    _validate_allowed_roots(cfg.log_path, "TELEDRIVE_ALLOWED_LOG_ROOTS")
    sqlite_path = sqlite_database_path(cfg.db_path)
    if sqlite_path is not None:
        _validate_allowed_roots(sqlite_path, "TELEDRIVE_ALLOWED_SQLITE_ROOTS")
    for account in cfg.accounts:
        _validate_allowed_roots(account.session_path, "TELEDRIVE_ALLOWED_SESSION_ROOTS")


def _renew_lease_now(conn: Database) -> None:
    if not conn.renew_lease(LEASE_NAME, RUN_HOLDER, int(time.time()), LEASE_TTL_SECONDS):
        raise RuntimeError("Distributed uploader lease was lost")


async def _lease_renewer(conn: Database, lost: asyncio.Event) -> None:
    interval = max(5, LEASE_TTL_SECONDS // 3)
    while True:
        try:
            await asyncio.sleep(interval)
            ok = conn.renew_lease(LEASE_NAME, RUN_HOLDER, int(time.time()), LEASE_TTL_SECONDS)
            if not ok:
                lost.set()
                logging.critical("Lost distributed uploader lease; stopping before the next file.")
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            lost.set()
            logging.exception("Distributed lease renewal failed: %s", exc)
            return


async def _run_with_distributed_lease(
    cfg: AppConfig,
    conn: Database,
    *,
    no_scan: bool,
    scan_only: bool,
    run_once: bool,
    login_code: Optional[str],
    login_password: Optional[str],
    auth_dir: Optional[str],
) -> int:
    if not conn.acquire_lease(LEASE_NAME, RUN_HOLDER, int(time.time()), LEASE_TTL_SECONDS):
        raise RuntimeError("Another TeleDrive uploader instance currently holds the distributed lease")

    lost = asyncio.Event()
    renew_task = asyncio.create_task(_lease_renewer(conn, lost))
    try:
        if not no_scan:
            inserted, updated, unchanged = scan_and_queue(
                conn,
                cfg,
                lease_heartbeat=lambda: _renew_lease_now(conn),
            )
            logging.info("Scan complete. inserted=%s updated=%s unchanged=%s", inserted, updated, unchanged)

        if scan_only and not run_once:
            return 0

        if lost.is_set():
            raise RuntimeError("Distributed uploader lease was lost before upload start")

        await process_uploads(
            cfg, conn,
            login_code=login_code,
            login_password=login_password,
            auth_dir=auth_dir,
            lease_lost=lost,
        )
        return 0
    finally:
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass
        try:
            conn.release_lease(LEASE_NAME, RUN_HOLDER)
        except Exception:
            logging.exception("Failed to release distributed uploader lease")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Safe Telegram auto uploader")
    p.add_argument("--config", required=True, help="Path to YAML config")
    p.add_argument("--scan-only", action="store_true", help="Only scan and queue files, then exit")
    p.add_argument("--run-once", action="store_true", help="Run one upload batch and exit")
    p.add_argument("--no-scan", action="store_true", help="Skip scan phase before upload")
    p.add_argument("--queue-object-bucket", metavar="BUCKET",
                   help="Scan legacy source into the durable object queue; never upload in this mode")
    p.add_argument("--queue-object-prefix", default="",
                   help="Relative object-key prefix for --queue-object-bucket")
    p.add_argument("--queue-object-limit", type=int, default=1000,
                   help="Maximum new durable jobs from one scan (1..1000)")
    p.add_argument("--auth-dir", help=argparse.SUPPRESS)
    return p.parse_args()


async def async_main() -> int:
    os.umask(0o077)
    args = parse_args()
    cfg = load_config(args.config)
    validate_config(cfg)
    setup_logging(cfg)
    conn = connect_db(cfg.db_path)

    try:
        with uploader_lock(cfg.db_path):
            if args.queue_object_bucket:
                # Deliberate opt-in migration: no Telethon connection, and
                # never process_uploads() while creating durable jobs.
                if args.no_scan or args.run_once:
                    raise ValueError("Object-queue import cannot run --no-scan or --run-once")
                from teledrive.storage.catalog import BucketCatalog
                from teledrive.storage.manifest import ManifestStore
                from teledrive.storage.jobs import StorageUploadJobs
                from teledrive.storage.legacy_bridge import enqueue_legacy_folder
                if not conn.acquire_lease(
                    LEASE_NAME, RUN_HOLDER, int(time.time()), LEASE_TTL_SECONDS
                ):
                    raise RuntimeError("Legacy uploader active; stop it before object-queue import")
                try:
                    queue = StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
                    report = enqueue_legacy_folder(
                        cfg, queue, bucket=args.queue_object_bucket,
                        prefix=args.queue_object_prefix,
                        max_files=args.queue_object_limit,
                        lease_heartbeat=lambda: (
                            conn.renew_lease(
                                LEASE_NAME, RUN_HOLDER, int(time.time()),
                                LEASE_TTL_SECONDS,
                            ) or (_ for _ in ()).throw(
                                RuntimeError("Legacy uploader lease lost during import")
                            )
                        ),
                    )
                    logging.info(
                        "Durable import: queued=%d existing=%d excluded=%d bucket=%s",
                        report.queued, report.already_queued,
                        report.excluded, args.queue_object_bucket,
                    )
                    return 0
                finally:
                    conn.release_lease(LEASE_NAME, RUN_HOLDER)
            return await _run_with_distributed_lease(
                cfg,
                conn,
                no_scan=args.no_scan,
                scan_only=args.scan_only,
                run_once=args.run_once,
                login_code=None,
                login_password=None,
                auth_dir=args.auth_dir,
            )
    finally:
        conn.close()


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
