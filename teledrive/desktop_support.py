"""UI-neutral desktop configuration and Telegram login handoff helpers."""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any

from .uploader import _safe_account_token

AUTH_PROMPT = re.compile(
    r"Please enter the (code|password) for account '([A-Za-z0-9_.-]{1,64})':",
    re.IGNORECASE,
)
ACCOUNT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


def parse_auth_prompt(line: str) -> tuple[str, str] | None:
    """Return (account, kind); ignore unrelated output."""
    match = AUTH_PROMPT.search(line)
    if not match:
        return None
    return match.group(2), match.group(1).lower()


def write_auth_response(directory: Path, account: str, kind: str, value: str) -> Path:
    """Atomically deliver a code/2FA password to exactly one account."""
    if not ACCOUNT_NAME.fullmatch(account) or kind not in {"code", "password"}:
        raise ValueError("Invalid Telegram login prompt")
    if not value.strip() or len(value) > 1024:
        raise ValueError("Invalid Telegram authentication value")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Authentication directory cannot be a symlink")
    destination = directory / f"{_safe_account_token(account)}_{kind}.txt"
    temporary = directory / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value.strip())
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def accounts_from_config(telegram: dict[str, Any]) -> list[dict[str, Any]]:
    """Return editable accounts, upgrading legacy single-account configuration."""
    accounts = telegram.get("accounts")
    if isinstance(accounts, list) and accounts:
        return [dict(item) for item in accounts]
    return [{
        "name": str(telegram.get("name") or "default"),
        "enabled": True,
        "phone": str(telegram.get("phone") or ""),
        "target": telegram.get("target", "me"),
        "session_path": str(telegram.get("session_path") or "data/telethon_user"),
    }]


def validate_account_rows(rows: list[dict[str, Any]], default_api_id: int, default_api_hash: str) -> None:
    if not rows or not any(row.get("enabled", True) for row in rows):
        raise ValueError("Enable at least one Telegram account.")
    names: set[str] = set()
    sessions: set[str] = set()
    for row in rows:
        name = str(row.get("name", "")).strip()
        if not ACCOUNT_NAME.fullmatch(name):
            raise ValueError("Account names must be 1–64 letters, digits, dots, '_' or '-'.")
        if name in names:
            raise ValueError(f"Duplicate account name: {name}")
        names.add(name)
        session = str(row.get("session_path") or "").strip()
        if not session or session in sessions:
            raise ValueError("Each account must have a unique session path.")
        sessions.add(session)
        if row.get("enabled", True):
            if int(row.get("api_id") or default_api_id) <= 0:
                raise ValueError(f"Account {name} needs a valid API ID.")
            if not str(row.get("api_hash") or default_api_hash).strip():
                raise ValueError(f"Account {name} needs a valid API hash.")
