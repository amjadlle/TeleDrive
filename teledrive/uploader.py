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
RUN_HOLDER = ""
LEASE_NAME = ""
LEASE_TTL_SECONDS = 0
CLAIM_STALE_SECONDS = int(os.environ.get("TELEDRIVE_CLAIM_STALE_SECONDS", "86400"))
SPLIT_MIN_FREE_BYTES = int(os.environ.get("TELEDRIVE_SPLIT_MIN_FREE_BYTES", str(512 * 1024 * 1024)))


@dataclass(frozen=True)
@dataclass
class AppConfig:
    api_id: int
    api_hash: str
    phone: Optional[str]
    target: Union[str, int]
    session_path: str
    strategy: str
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

    # Legacy direct-send scanner is gone; the unified service uses
    # storage.apis for credentials. Tolerate a legacy telegram: section but
    # do not parse accounts.
    return AppConfig(
        api_id=default_api_id,
        api_hash=default_api_hash,
        phone=None,
        target=default_target,
        session_path=config_path("data/telethon_user"),
        strategy=strategy,
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
        log_path=config_path(lg.get("log_path", "/data/uploader.log")),
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
    # Guard against attribute traversal via field_name like "name.__class__"
    return template.format(name=name, stem=stem, ext=ext)[:1024]



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

