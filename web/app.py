from __future__ import annotations

import asyncio
import hmac
import copy
import sys
import tempfile
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field
from typing import Literal
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from teledrive import uploader
from teledrive.db import redact_database_url
from teledrive.api_keys import ApiKeyStore
from teledrive.s3.credentials import S3CredentialStore, load_master_key, DEFAULT_PERMISSIONS
from teledrive.s3.multipart import MultipartStore
from teledrive.storage.gc import StorageGCInventory
from teledrive.storage.manifest import ManifestStore
from teledrive.storage.jobs import StorageUploadJobs, UploadJobError, jobs as storage_job_rows
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.copy import copy_object, CopyConflict
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.bot_api_adapter import (
    BotApiClient, BotApiChunkTransport, BotFileIndex, BOT_RECOMMENDED_CHUNK_SIZE,
)
from teledrive.storage.user_mode import (
    UserSessionRuntime, UserSendJournal, UnifiedUserTransport, transport_mode,
)
from teledrive.storage.history_inventory import inventory_channel
mtproto_storage_runtime = UserSessionRuntime()

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("TELEDRIVE_CONFIG", "/config/config.yaml"))
AUTH_DIR = Path(os.environ.get("TELEDRIVE_AUTH_DIR", "/data/auth"))
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _load_web_password() -> str:
    secret_file = os.environ.get("TELEDRIVE_WEB_PASSWORD_FILE", "").strip()
    if secret_file:
        path = Path(secret_file)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(path), flags)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                value = handle.read(4097)
        except OSError as exc:
            raise RuntimeError("Unable to safely read TELEDRIVE_WEB_PASSWORD_FILE") from exc
        if len(value) > 4096:
            raise RuntimeError("TELEDRIVE_WEB_PASSWORD_FILE is unexpectedly large")
        return value.rstrip("\r\n")
    return os.environ.get("TELEDRIVE_WEB_PASSWORD", "")


WEB_USER = os.environ.get("TELEDRIVE_WEB_USER", "admin")
WEB_PASSWORD = _load_web_password()
ALLOW_INSECURE = os.environ.get("TELEDRIVE_ALLOW_INSECURE", "").strip().lower() in {"1", "true", "yes"}
REDACTED = "__REDACTED__"
MAX_REQUEST_BYTES = int(os.environ.get("TELEDRIVE_MAX_REQUEST_BYTES", "1048576"))
ALLOWED_HOSTS = [
    item.strip()
    for item in os.environ.get("TELEDRIVE_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    if item.strip()
]
LOG_BUFFER: deque[str] = deque(maxlen=1500)
PROMPT_RE = re.compile(r"Please enter the (code|password) for account '([^']+)':", re.I)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:password|passwd|pwd|token|access_token|secret|api_key|apikey)=)[^&#\s]+"
)

security = HTTPBasic(auto_error=False)
bearer_auth = HTTPBearer(auto_error=False)
process_lock = asyncio.Lock()
current_process: asyncio.subprocess.Process | None = None
current_mode: str | None = None
current_started_at: int | None = None
current_prompt: dict[str, str] | None = None
scheduler_task: asyncio.Task | None = None
AUTH_FAILURES: dict[str, deque[float]] = {}
AUTH_FAILURE_WINDOW_SECONDS = int(os.environ.get("TELEDRIVE_AUTH_FAILURE_WINDOW_SECONDS", "60"))
AUTH_FAILURE_LIMIT = int(os.environ.get("TELEDRIVE_AUTH_FAILURE_LIMIT", "5"))


def _secure_mkdir(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked security-sensitive directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    except OSError:
        # Permission hardening may be unavailable on some non-POSIX hosts.
        pass


def _secure_write(path: Path, content: str) -> None:
    _secure_mkdir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _scrub_text(value: str) -> str:
    value = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://[^:/@\s]+:)[^@\s]+(@)", r"\1***\2", value)
    return _QUERY_SECRET_RE.sub(r"\1***", value)


def _same_origin(request: Request) -> None:
    if request.headers.get("x-teledrive-csrf") != "1":
        raise HTTPException(403, "Missing CSRF protection header")
    origin = request.headers.get("origin")
    if not origin:
        return
    host = request.headers.get("host", "").lower()
    try:
        origin_host = urlsplit(origin).netloc.lower()
    except Exception:
        raise HTTPException(403, "Invalid Origin header")
    if not host or origin_host != host:
        raise HTTPException(403, "Cross-origin state change rejected")


def require_auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not WEB_PASSWORD:
        return

    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    failures = AUTH_FAILURES.setdefault(client, deque())
    while failures and now - failures[0] > AUTH_FAILURE_WINDOW_SECONDS:
        failures.popleft()
    if len(failures) >= AUTH_FAILURE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many authentication failures")

    valid = credentials is not None and (
        hmac.compare_digest(credentials.username, WEB_USER)
        and hmac.compare_digest(credentials.password, WEB_PASSWORD)
    )
    if not valid:
        failures.append(now)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Basic realm="TeleDrive"'},
        )

    AUTH_FAILURES.pop(client, None)


def default_config() -> dict[str, Any]:
    return {
        "telegram": {
            "strategy": "parallel",
            "api_id": 0,
            "api_hash": "",
            "target": "me",
            "accounts": [{
                "name": "primary",
                "enabled": True,
                "phone": "",
                "session_path": "/data/sessions/primary",
            }],
        },
        "upload": {
            "source_dir": "/uploads",
            "allowed_extensions": [],
            "max_file_size_mb": 0,
            "sleep_min_seconds": 60,
            "sleep_max_seconds": 90,
            "max_files_per_run": 12,
            "max_files_per_day": 100,
            "retry_attempts": 5,
            "backoff_base_seconds": 10,
            "floodwait_buffer_seconds": 5,
            "caption_template": "{name}",
            "send_mode": "document",
        },
        "state": {"database_url": "sqlite:////data/state.db"},
        "logging": {"log_path": "/data/uploader.log", "level": "INFO"},
        "app": {"auto_run": False, "run_interval_minutes": 60},
    }


def ensure_config() -> None:
    _secure_mkdir(CONFIG_PATH.parent)
    if CONFIG_PATH.is_symlink():
        raise RuntimeError("Refusing to use a symlinked TeleDrive config file")
    _secure_mkdir(AUTH_DIR)
    _secure_mkdir(Path("/data/sessions"))
    _secure_mkdir(Path("/data/tmp"))
    Path("/uploads").mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        _secure_write(CONFIG_PATH, yaml.safe_dump(default_config(), sort_keys=False))
    else:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(CONFIG_PATH), flags)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        except OSError as exc:
            raise RuntimeError("Unable to safely open TeleDrive config file") from exc


def read_raw_config() -> dict[str, Any]:
    ensure_config()
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def _redacted_config(raw: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(raw)
    tg = result.get("telegram", {})
    if tg.get("api_hash"):
        tg["api_hash"] = REDACTED
    for account in tg.get("accounts", []) or []:
        if isinstance(account, dict) and account.get("api_hash"):
            account["api_hash"] = REDACTED
    state_cfg = result.get("state", {})
    db_url = state_cfg.get("database_url") or state_cfg.get("db_path")
    if db_url:
        state_cfg["database_url"] = redact_database_url(str(db_url))
        state_cfg.pop("db_path", None)
    return result


def _restore_secrets(candidate: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(candidate)
    new_tg = result.setdefault("telegram", {})
    old_tg = current.get("telegram", {})
    if new_tg.get("api_hash") == REDACTED:
        new_tg["api_hash"] = old_tg.get("api_hash", "")

    old_accounts = {
        str(a.get("name")): a
        for a in (old_tg.get("accounts") or [])
        if isinstance(a, dict) and a.get("name")
    }
    for account in new_tg.get("accounts", []) or []:
        if not isinstance(account, dict):
            continue
        if account.get("api_hash") == REDACTED:
            account["api_hash"] = old_accounts.get(str(account.get("name")), {}).get("api_hash", "")

    new_state = result.setdefault("state", {})
    old_state = current.get("state", {})
    old_url = old_state.get("database_url") or old_state.get("db_path")
    submitted = new_state.get("database_url")
    if old_url and submitted == redact_database_url(str(old_url)):
        new_state["database_url"] = old_url
    return result


def effective_config():
    ensure_config()
    return uploader.load_config(str(CONFIG_PATH))


def validate_and_write_config(raw: dict[str, Any]) -> None:
    current = read_raw_config()
    merged = _restore_secrets(raw, current)
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    _secure_write(tmp, yaml.safe_dump(merged, sort_keys=False))
    try:
        cfg = uploader.load_config(str(tmp))
        uploader.validate_config(cfg)
        conn = uploader.connect_db(cfg.db_path)
        conn.close()
    except ValueError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Database connection/initialization failed") from exc
    os.replace(tmp, CONFIG_PATH)


def db_connect():
    cfg = effective_config()
    uploader.validate_storage_paths(cfg)
    return uploader.connect_db(cfg.db_path)


def _cleanup_auth_files() -> None:
    if not AUTH_DIR.exists():
        return
    for pattern in ("*_code.txt", "*_password.txt", "*.tmp"):
        for path in AUTH_DIR.glob(pattern):
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def process_snapshot() -> dict[str, Any]:
    proc = current_process
    return {
        "running": proc is not None and proc.returncode is None,
        "pid": proc.pid if proc is not None and proc.returncode is None else None,
        "mode": current_mode,
        "started_at": current_started_at,
        "auth_prompt": current_prompt,
    }


async def _capture_process(proc: asyncio.subprocess.Process) -> None:
    global current_process, current_mode, current_started_at, current_prompt
    assert proc.stdout is not None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            message = line.decode("utf-8", errors="replace").rstrip()
            if message:
                LOG_BUFFER.append(_scrub_text(message))
                match = PROMPT_RE.search(message)
                if match:
                    current_prompt = {"kind": match.group(1).lower(), "account": match.group(2)}
        await proc.wait()
        LOG_BUFFER.append(f"[web] uploader exited with code {proc.returncode}")
    finally:
        async with process_lock:
            if current_process is proc:
                current_process = None
                current_mode = None
                current_started_at = None
                current_prompt = None
                _cleanup_auth_files()


async def start_uploader(mode: str) -> dict[str, Any]:
    global current_process, current_mode, current_started_at, current_prompt
    if mode not in {"run-once", "scan-only", "connect"}:
        raise HTTPException(400, "mode must be run-once, scan-only, or connect")
    async with process_lock:
        if current_process is not None and current_process.returncode is None:
            raise HTTPException(409, "Uploader is already running")
        if mode != "scan-only" and os.environ.get("TELEDRIVE_JOB_LEGACY_AUTO_BUCKET", "").strip():
            raise HTTPException(409, "Legacy uploader sends disabled during durable cutover")
        args = [sys.executable, "-m", "teledrive.uploader", "--config", str(CONFIG_PATH), "--auth-dir", str(AUTH_DIR)]
        if mode == "scan-only":
            args.append("--scan-only")
        elif mode == "connect":
            args.extend(["--run-once", "--no-scan"])
        else:
            args.append("--run-once")
        _cleanup_auth_files()
        current_prompt = None
        current_mode = mode
        current_started_at = int(time.time())
        LOG_BUFFER.append("[web] $ " + " ".join(args))
        child_env = os.environ.copy()
        for secret_name in ("TELEDRIVE_WEB_PASSWORD", "TELEDRIVE_WEB_PASSWORD_FILE"):
            child_env.pop(secret_name, None)
        current_process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(BASE_DIR),
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(_capture_process(current_process))
        return process_snapshot()


async def stop_uploader() -> dict[str, Any]:
    proc = current_process
    if proc is None or proc.returncode is not None:
        return process_snapshot()
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    return process_snapshot()


async def scheduler_loop() -> None:
    last_launch = 0.0
    while True:
        try:
            app_cfg = read_raw_config().get("app", {})
            enabled = bool(app_cfg.get("auto_run", False))
            interval = max(1, int(app_cfg.get("run_interval_minutes", 60))) * 60
            now = time.time()
            running = current_process is not None and current_process.returncode is None
            if (enabled and os.environ.get("TELEDRIVE_JOB_LEGACY_AUTO_BUCKET", "").strip()):
                raise RuntimeError("Disable legacy auto_run before durable cutover")
            if enabled and not running and now - last_launch >= interval:
                await start_uploader("run-once")
                last_launch = now
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG_BUFFER.append(_scrub_text(f"[web] scheduler error: {exc}"))
            await asyncio.sleep(10)


def _legacy_cutover_configuration() -> tuple[str, str, int] | None:
    """No implicit legacy upload mode migration; explicit environment opt-in."""
    bucket = os.environ.get("TELEDRIVE_JOB_LEGACY_AUTO_BUCKET", "").strip()
    if not bucket:
        return None
    if os.environ.get("TELEDRIVE_JOB_AUTO_RUN") != "1":
        raise RuntimeError("Legacy cutover requires TELEDRIVE_JOB_AUTO_RUN=1")
    if bool(read_raw_config().get("app", {}).get("auto_run", False)):
        raise RuntimeError("Disable legacy app.auto_run before durable cutover")
    prefix = os.environ.get("TELEDRIVE_JOB_LEGACY_AUTO_PREFIX", "").strip()
    try:
        limit = int(os.environ.get("TELEDRIVE_JOB_LEGACY_AUTO_LIMIT", "100"))
    except ValueError as exc:
        raise RuntimeError("Invalid legacy cutover scan limit") from exc
    if not 1 <= limit <= 1000:
        raise RuntimeError("Legacy cutover scan limit must be 1..1000")
    return bucket, prefix, limit


async def upload_job_scheduler_loop() -> None:
    """Opt-in durable job worker; SQL leases prevent duplicate claims.

    Disabled unless TELEDRIVE_JOB_AUTO_RUN=1. Every cycle gets its own
    connection; failures retain job metadata for explicit operator retry.
    """
    while True:
        conn = None
        try:
            if os.environ.get("TELEDRIVE_JOB_AUTO_RUN") != "1":
                await asyncio.sleep(10)
                continue
            cutover = _legacy_cutover_configuration()
            root = _storage_job_root()
            conn = db_connect()
            queue = StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
            scratch = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
            _secure_mkdir(scratch)
            if cutover is not None:
                from teledrive.storage.legacy_bridge import run_legacy_cutover_cycle
                cfg = effective_config()
                lease = db_connect()
                try:
                    # The cycle manages an independently renewable lease while
                    # Telegram chunks are being sent by the durable queue.
                    _, result = await run_legacy_cutover_cycle(
                        cfg, queue, storage_bot_transport(conn), lease_db=lease,
                        bucket=cutover[0], prefix=cutover[1], max_files=cutover[2],
                        allowed_root=root, scratch_directory=scratch,
                        lease_name=uploader.LEASE_NAME,
                        lease_ttl=uploader.LEASE_TTL_SECONDS,
                        chunk_size=BOT_RECOMMENDED_CHUNK_SIZE,
                    )
                finally:
                    lease.close()
            else:
                result = await queue.run_next(
                    storage_bot_transport(conn), allowed_root=root,
                    scratch_directory=scratch, chunk_size=BOT_RECOMMENDED_CHUNK_SIZE,
                )
            await asyncio.sleep(2 if result else 10)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not log source paths, credentials or Telegram tokens.
            LOG_BUFFER.append(
                "[web] durable upload job scheduler: " + type(exc).__name__
            )
            await asyncio.sleep(20)
        finally:
            if conn is not None:
                conn.close()


async def lifecycle_scheduler_loop() -> None:
    """Apply bounded SQL-only lifecycle rules hourly, in this one process."""
    while True:
        connection = None
        try:
            from teledrive.s3.lifecycle import BucketLifecycleStore
            connection = db_connect()
            cat = BucketCatalog(ManifestStore(connection))
            BucketLifecycleStore(cat).apply_due(now=int(time.time()), limit=100)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG_BUFFER.append("[web] lifecycle scheduler: " + type(exc).__name__)
        finally:
            if connection is not None:
                connection.close()
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global scheduler_task, upload_job_scheduler_task, lifecycle_scheduler_task
    os.umask(0o077)
    if not WEB_PASSWORD and not ALLOW_INSECURE:
        raise RuntimeError(
            "TELEDRIVE_WEB_PASSWORD is required. Set TELEDRIVE_ALLOW_INSECURE=1 only for explicitly trusted local deployments."
        )
    if WEB_PASSWORD and len(WEB_PASSWORD) < 16:
        raise RuntimeError("TELEDRIVE_WEB_PASSWORD must be at least 16 characters")
    ensure_config()
    if transport_mode() == "telethon":
        await mtproto_storage_runtime.start()
    try:
        conn = db_connect()
        conn.close()
    except Exception as exc:
        LOG_BUFFER.append(_scrub_text(f"[web] startup database warning: {exc}"))
    scheduler_task = asyncio.create_task(scheduler_loop())
    upload_job_scheduler_task = asyncio.create_task(upload_job_scheduler_loop())
    lifecycle_scheduler_task = asyncio.create_task(lifecycle_scheduler_loop())
    yield
    if scheduler_task:
        scheduler_task.cancel()
    if upload_job_scheduler_task:
        upload_job_scheduler_task.cancel()
        await asyncio.gather(upload_job_scheduler_task, return_exceptions=True)
    if lifecycle_scheduler_task:
        lifecycle_scheduler_task.cancel()
        await asyncio.gather(lifecycle_scheduler_task, return_exceptions=True)
    await stop_uploader()
    await mtproto_storage_runtime.stop()


app = FastAPI(
    title="TeleDrive API",
    version="2.1.0",
    description=(
        "Authenticated management API for Telegram upload accounts, queue/history, "
        "application configuration, execution and Telegram login challenges. "
        "Use HTTP Basic credentials and set X-TeleDrive-CSRF: 1 for state-changing requests. "
        "Protect access with HTTPS or a trusted VPN."
    ),
    openapi_tags=[
        {"name": "System", "description": "Service state and health"},
        {"name": "Configuration", "description": "Settings, Telegram accounts, and database URL"},
        {"name": "Uploads", "description": "Queue/history and uploader lifecycle"},
        {"name": "Authentication", "description": "Telegram code and two-factor password handoff"},
        {"name": "API keys", "description": "Root-admin API key creation, rotation and revocation"},
        {"name": "Object catalog", "description": "Bearer-scoped bucket and object metadata"},
    ],
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    binary_upload = (request.method == "PUT" and
                     re.fullmatch(r"/api/v1/buckets/[A-Za-z0-9.-]+/content", request.url.path))
    if request.method in {"POST", "PUT", "PATCH"} and not binary_upload:
        # Size-bound browser JSON requests. Cache on the ORIGINAL request:
        # Starlette's call_next() replays _CachedRequest's cached body.
        # Constructing a new Request here silently discards POST/PUT payloads.
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_REQUEST_BYTES:
                    return JSONResponse(status_code=413, content={"detail": "Request body too large"})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        request._body = bytes(body)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path in {"/docs", "/redoc"}:
        # FastAPI's official interactive documentation assets are served from the CDN.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "object-src 'none'; connect-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'"
        )
    if request.url.path in {"/", "/docs", "/redoc", "/openapi.json"} or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class StartRequest(BaseModel):
    mode: Literal["run-once", "scan-only", "connect"] = Field(
        default="run-once", description="Run upload batch, scan only, or connect accounts."
    )


class TelegramAuthRequest(BaseModel):
    account: str = Field(min_length=1, max_length=64, examples=["primary"])
    kind: Literal["code", "password"]
    value: str = Field(min_length=1, max_length=1024, description="Telegram code or 2FA password; never logged.")


class OKResponse(BaseModel):
    ok: bool = True


class KeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1, max_length=20)
    expires_at: int | None = Field(default=None, description="Optional Unix expiry time")


class S3KeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    permissions: list[str] = Field(default_factory=lambda: list(DEFAULT_PERMISSIONS))
    expires_at: int | None = None


class S3CompletionRecoveryRequest(BaseModel):
    execute: bool = False
    min_age_seconds: int = Field(default=3600, ge=60, le=30 * 86400)


class PendingUploadRecoveryRequest(BaseModel):
    execute: bool = False
    min_age_seconds: int = Field(default=3600, ge=60, le=30 * 86400)


class StorageUploadJobRequest(BaseModel):
    bucket: str = Field(min_length=3, max_length=63)
    key: str = Field(min_length=1, max_length=1024)
    source_path: str = Field(min_length=1, max_length=4096)


class LegacyFolderImportRequest(BaseModel):
    bucket: str = Field(min_length=3, max_length=63)
    prefix: str = Field(default="", max_length=512)
    max_files: int = Field(default=100, ge=1, le=1000)
    execute: bool = False


class VersionRestoreRequest(BaseModel):
    bucket: str = Field(min_length=3, max_length=63)
    key: str = Field(min_length=1, max_length=1024)
    expected_generation: int | None = Field(default=None, ge=0)


class VersionIndexRequest(BaseModel):
    execute: bool = False
    limit: int = Field(default=100, ge=1, le=1000)


class BucketCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=63)

def storage_bot_transport(conn):
    """Shared REST/S3/job transport; keep this name for existing integrations."""
    token = os.environ.get("TELEDRIVE_STORAGE_BOT_TOKEN", "").strip()
    chat_raw = os.environ.get("TELEDRIVE_STORAGE_BOT_CHAT_ID", "").strip()
    bot = None
    if token and chat_raw:
        try:
            bot = BotApiChunkTransport(
                BotApiClient(token), BotFileIndex(conn), chat_id=int(chat_raw),
                account="api-bot",
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(503, "Telegram Bot storage configuration invalid") from exc
    if transport_mode() == "telethon":
        try:
            mtproto_storage_runtime.require()
            return UnifiedUserTransport(
                mtproto_storage_runtime, UserSendJournal(conn), bot,
            )
        except RuntimeError as exc:
            raise HTTPException(503, "Telegram user storage session not ready") from exc
    if bot is None:
        raise HTTPException(503, "REST object transfer requires Telegram Bot credentials")
    return bot


def _byte_range(value: str | None, size: int) -> tuple[int, int, bool]:
    """HTTP Range with inclusive input -> half-open output; 416 on invalid input."""
    if value is None:
        return 0, size, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or not (match.group(1) or match.group(2)):
        raise HTTPException(416, "Unsupported byte range", headers={"Content-Range": f"bytes */{size}"})
    first, last = match.groups()
    if size == 0:
        raise HTTPException(416, "Empty object has no satisfiable range",
                            headers={"Content-Range": "bytes */0"})
    if not first:
        count = int(last)
        if count <= 0:
            raise HTTPException(416, "Invalid suffix range", headers={"Content-Range": f"bytes */{size}"})
        start, end = max(0, size - count), size
    else:
        start = int(first)
        end = min(size, int(last) + 1) if last else size
        if start >= size or end <= start:
            raise HTTPException(416, "Unsatisfiable range", headers={"Content-Range": f"bytes */{size}"})
    return start, end, True



def require_api_scope(required: str):
    """HTTP bearer authorization, independent of the browser Basic/CSRF flow."""
    def check(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_auth)) -> None:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(401, "API Bearer token required",
                                headers={"WWW-Authenticate": "Bearer"})
        conn = db_connect()
        try:
            authorized = ApiKeyStore(conn).authorize(credentials.credentials, required)
        finally:
            conn.close()
        if not authorized:
            raise HTTPException(403, "API token is invalid, expired or lacks permission")
    return check




app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/openapi.json", tags=["System"], include_in_schema=False)
def openapi_schema(_: None = Depends(require_auth)):
    return JSONResponse(app.openapi())


@app.get("/docs", tags=["System"], include_in_schema=False)
def swagger_docs(_: None = Depends(require_auth)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="TeleDrive API · Swagger UI")


@app.get("/redoc", tags=["System"], include_in_schema=False)
def redoc_docs(_: None = Depends(require_auth)):
    return get_redoc_html(openapi_url="/openapi.json", title="TeleDrive API · ReDoc")


@app.get("/", include_in_schema=False)
def index(_: None = Depends(require_auth)):
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config", tags=["Configuration"], summary="Read configuration (secrets redacted)")
def get_config(_: None = Depends(require_auth)):
    return _redacted_config(read_raw_config())


@app.put("/api/config", tags=["Configuration"], response_model=OKResponse, summary="Validate and save configuration")
def put_config(request: Request, payload: dict[str, Any] = Body(...), _: None = Depends(require_auth)):
    _same_origin(request)
    if current_process is not None and current_process.returncode is None:
        raise HTTPException(409, "Stop the uploader before changing configuration")
    try:
        validate_and_write_config(payload)
    except ValueError as exc:
        raise HTTPException(400, _scrub_text(str(exc))) from exc
    except Exception as exc:
        raise HTTPException(400, "Configuration/database validation failed") from exc
    return {"ok": True}


@app.get("/api/dashboard", tags=["System"], summary="Read queue metrics and Telegram account status")
def dashboard(_: None = Depends(require_auth)):
    cfg = effective_config()
    conn = db_connect()
    try:
        counts = conn.dashboard_counts()
        uploaded_today = uploader.uploaded_today(conn)
        statuses = conn.all_account_states()
        account_counts = conn.uploaded_counts_by_account()
    finally:
        conn.close()

    accounts = []
    for account in cfg.accounts:
        state = statuses.get(account.name, {})
        accounts.append({
            "name": account.name,
            "enabled": account.enabled,
            "phone": account.phone or "",
            "target": account.target,
            "session_path": account.session_path,
            "state": state.get("state", "unknown"),
            "last_error": state.get("last_error"),
            "cooldown_until": state.get("cooldown_until"),
            "last_success_ts": state.get("last_success_ts"),
            "last_update_ts": state.get("last_update_ts"),
            "uploaded_count": account_counts.get(account.name, 0),
        })

    return {
        "strategy": cfg.strategy,
        "database_url": redact_database_url(cfg.db_path),
        "queue": {
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "uploaded": counts.get("uploaded", 0),
            "uploaded_today": uploaded_today,
        },
        "accounts": accounts,
        "process": process_snapshot(),
    }


@app.get("/api/files", tags=["Uploads"], summary="List queued or uploaded files")
def files_endpoint(
    kind: str = Query("queue", pattern="^(queue|history)$"),
    limit: int = Query(100, ge=1, le=1000),
    _: None = Depends(require_auth),
):
    conn = db_connect()
    try:
        return conn.list_files(kind, limit)
    finally:
        conn.close()


@app.post("/api/process/start", tags=["Uploads"], summary="Start uploader process")
async def process_start(request: Request, payload: StartRequest = Body(default=StartRequest()), _: None = Depends(require_auth)):
    _same_origin(request)
    return await start_uploader(payload.mode)


@app.post("/api/process/stop", tags=["Uploads"], summary="Stop uploader process")
async def process_stop(request: Request, _: None = Depends(require_auth)):
    _same_origin(request)
    return await stop_uploader()


@app.post("/api/auth", tags=["Authentication"], response_model=OKResponse, summary="Respond to a pending Telegram login prompt")
def submit_auth(request: Request, payload: TelegramAuthRequest, _: None = Depends(require_auth)):
    global current_prompt
    _same_origin(request)
    account = payload.account.strip()
    kind = payload.kind
    value = payload.value.strip()
    if not account or kind not in {"code", "password"} or not value:
        raise HTTPException(400, "account, kind(code/password), and value are required")
    if len(value) > 1024:
        raise HTTPException(400, "Auth value is too long")
    if not current_prompt or current_prompt.get("account") != account or current_prompt.get("kind") != kind:
        raise HTTPException(409, "No matching Telegram authentication prompt is active")
    _secure_mkdir(AUTH_DIR)
    token = uploader._safe_account_token(account)
    path = AUTH_DIR / f"{token}_{kind}.txt"
    tmp = path.with_suffix(path.suffix + ".tmp")
    _secure_write(tmp, value)
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    current_prompt = None
    return {"ok": True}


@app.get("/api/logs", tags=["System"], summary="Read recent redacted activity logs")
def logs(limit: int = Query(300, ge=1, le=1500), _: None = Depends(require_auth)):
    cfg = effective_config()
    combined = list(LOG_BUFFER)[-limit:]
    log_path = Path(cfg.log_path)
    if log_path.exists():
        try:
            combined = ([_scrub_text(line) for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]] + combined)[-limit:]
        except OSError:
            pass
    return {"lines": combined, "process": process_snapshot()}


@app.get("/api/v1/admin/keys", tags=["API keys"], summary="List API key metadata; never returns secrets")
def list_api_keys(_: None = Depends(require_auth)):
    conn = db_connect()
    try:
        return {"keys": ApiKeyStore(conn).list()}
    finally:
        conn.close()


@app.post("/api/v1/admin/keys", tags=["API keys"], status_code=201, summary="Issue a scoped API key (secret returned once)")
def issue_api_key(request: Request, payload: KeyCreateRequest, _: None = Depends(require_auth)):
    _same_origin(request)
    conn = db_connect()
    try:
        try:
            key = ApiKeyStore(conn).create(payload.name, payload.scopes, expires_at=payload.expires_at)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"id": key.id, "token": key.value, "prefix": key.prefix, "scopes": key.scopes}
    finally:
        conn.close()


@app.delete("/api/v1/admin/keys/{identifier}", tags=["API keys"], summary="Revoke an API key immediately")
def revoke_api_key(identifier: str, request: Request, _: None = Depends(require_auth)):
    _same_origin(request)
    conn = db_connect()
    try:
        if not ApiKeyStore(conn).revoke(identifier):
            raise HTTPException(404, "API key not found or already revoked")
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/v1/admin/keys/{identifier}/rotate", tags=["API keys"], summary="Rotate an API key; secret returned once")
def rotate_api_key(identifier: str, request: Request, _: None = Depends(require_auth)):
    _same_origin(request)
    conn = db_connect()
    try:
        try:
            key = ApiKeyStore(conn).rotate(identifier)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": key.id, "token": key.value, "prefix": key.prefix, "scopes": key.scopes}
    finally:
        conn.close()


@app.get("/api/v1/admin/storage/gc-candidates", tags=["Storage recovery"],
         summary="Retention-aware dry-run FAILED journal planning, never deletion")
def list_storage_gc_candidates(
    min_age_seconds: int = Query(default=7*86400, ge=86400, le=365*86400),
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_auth),
):
    conn = db_connect()
    try:
        catalog = BucketCatalog(ManifestStore(conn))
        try:
            candidates = StorageGCInventory(catalog.manifests).preview_failed_retention(
                now=int(time.time()), min_age_seconds=min_age_seconds, limit=limit
            )
        except RuntimeError as exc:
            raise HTTPException(409, "Reference graph is inconsistent; GC blocked") from exc
        return {
            "candidates": candidates,
            "dry_run": True, "physical_deletion_enabled": False,
            "remote_history_scan_required": True,
        }
    finally:
        conn.close()


@app.get("/api/v1/admin/storage/gc-audit", tags=["API keys"],
         summary="Audit Telegram object references before any physical cleanup")
def get_storage_gc_audit(
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_auth),
):
    """Root-admin-only, non-destructive inventory; never authorizes remote deletion."""
    from dataclasses import asdict

    conn = db_connect()
    try:
        # Also ensure the bucket/head metadata tables exist on a new DB.
        catalog = BucketCatalog(ManifestStore(conn))
        try:
            inventory = StorageGCInventory(catalog.manifests).inspect()
        except RuntimeError as exc:
            raise HTTPException(409, "Reference graph is inconsistent; GC blocked") from exc
        return {
            "total_manifests": inventory.total_manifests,
            "total_telegram_messages": inventory.total_telegram_messages,
            "unjournaled_bot_receipts": inventory.unjournaled_bot_receipts,
            "unattributed_bot_file_ids": inventory.unattributed_bot_file_ids,
            "unresolved_bot_intents": inventory.unresolved_bot_intents,
            "live_object_heads": inventory.live_object_heads,
            "copy_aliases": inventory.copy_aliases,
            "multipart_parts": inventory.multipart_parts,
            "eligible_for_deletion": 0,
            "dry_run": True,
            "physical_deletion_enabled": False,
            "protected_chunks": [
                asdict(row) for row in inventory.protected_chunks[:limit]
            ],
            "truncated": len(inventory.protected_chunks) > limit,
        }
    finally:
        conn.close()


def _storage_job_root() -> Path:
    """Durable jobs may read only an operator-configured local source root."""
    raw = os.environ.get("TELEDRIVE_JOB_SOURCE_ROOT", "").strip()
    if not raw:
        raise HTTPException(503, "Configure TELEDRIVE_JOB_SOURCE_ROOT for local upload jobs")
    try:
        root = Path(raw).resolve(strict=True)
    except OSError as exc:
        raise HTTPException(503, "Upload source root does not exist") from exc
    if not root.is_dir():
        raise HTTPException(503, "Upload source root must be a directory")
    return root


@app.post("/api/v1/admin/storage/versions/index-current",
          tags=["Storage versions"],
          summary="Dry-run or explicitly index only verified historical HEAD references")
def index_legacy_current_versions(
    request: Request, payload: VersionIndexRequest,
    _: None = Depends(require_auth),
):
    _same_origin(request)
    conn = db_connect()
    try:
        catalog = BucketCatalog(ManifestStore(conn))
        try:
            registered = catalog.index_legacy_current_heads(
                dry_run=not payload.execute, limit=payload.limit,
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ids": registered, "count": len(registered),
                "dry_run": not payload.execute,
                "indexed": bool(payload.execute and registered)}
    finally:
        conn.close()


@app.get("/api/v1/admin/storage/versions", tags=["Storage versions"],
         summary="Admin listing of published historical versions for restore")
def admin_list_published_versions(
    bucket: str = Query(..., min_length=3, max_length=63),
    key: str = Query(..., min_length=1, max_length=1024),
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_auth),
):
    conn = db_connect()
    try:
        catalog = BucketCatalog(ManifestStore(conn))
        if bucket not in catalog.list_buckets():
            raise HTTPException(404, "Bucket not found")
        try:
            current = catalog.get(bucket, key)
            generation = current.generation
        except FileNotFoundError:
            generation = 0
        return {"bucket": bucket, "key": key, "generation": generation,
                "versions": catalog.list_versions(bucket, key, limit=limit)}
    finally:
        conn.close()


@app.post("/api/v1/admin/storage/versions/{version_id}/restore",
          tags=["Storage versions"],
          summary="Restore a previously published immutable version without Telegram uploads")
def restore_published_version(
    version_id: str, request: Request, payload: VersionRestoreRequest,
    _: None = Depends(require_auth),
):
    _same_origin(request)
    conn = db_connect()
    try:
        catalog = BucketCatalog(ManifestStore(conn))
        try:
            head = catalog.get(payload.bucket, payload.key)
            generation, current_id = head.generation, head.item.id
        except FileNotFoundError:
            generation, current_id = 0, None
        if payload.expected_generation is not None and (
            payload.expected_generation != generation
        ):
            raise HTTPException(409, "Generation has changed since restore preview")
        try:
            restored = copy_object(
                catalog, payload.bucket, payload.key,
                payload.bucket, payload.key,
                source_version_id=version_id,
                expected_destination_generation=generation,
                expected_destination_object_id=current_id,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, "Published version does not exist") from exc
        except (CopyConflict, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "id": restored.object_id, "source_version_id": version_id,
            "etag": restored.etag, "size": restored.size,
            "generation": catalog.get(payload.bucket, payload.key).generation,
        }
    finally:
        conn.close()


@app.get("/api/v1/admin/storage/health", tags=["Storage jobs"],
         summary="Read storage jobs, stale leases and protected-media metrics")
def storage_operations_health(_: None = Depends(require_auth)):
    from sqlalchemy import func, select

    conn = db_connect()
    try:
        catalog = BucketCatalog(ManifestStore(conn))
        queue = StorageUploadJobs(catalog)
        counts = conn.connection.execute(
            select(storage_job_rows.c.state, func.count())
            .group_by(storage_job_rows.c.state)
        ).all()
        now = int(time.time())
        pending = catalog.manifests.stale_pending(now=now, limit=1000)
        completing = MultipartStore(catalog).stale_completions(now=now, limit=1000)
        try:
            gc = StorageGCInventory(catalog.manifests).inspect()
        except RuntimeError as exc:
            raise HTTPException(409, "Reference graph is inconsistent; GC blocked") from exc
        from teledrive.storage.bot_api_adapter import BotFileIndex
        bot_index = BotFileIndex(conn)
        unjournaled_bot_acks = bot_index.unjournaled_count()
        unresolved_bot_sends = bot_index.unresolved_count()
        explicit_bot_429_count = bot_index.total_explicit_429_observations()
        states = {state: int(count) for state, count in counts}
        alerts = []
        if unresolved_bot_sends:
            alerts.append({
                "code": "unresolved_bot_sends", "severity": "critical",
                "count": unresolved_bot_sends,
                "message": "Bot sends have unknown acceptance; do not retry until owner-verified reconciliation",
            })
        if unjournaled_bot_acks:
            alerts.append({
                "code": "unjournaled_bot_acks", "severity": "warning",
                "count": unjournaled_bot_acks,
                "message": "Bot API ACK receipts not yet linked to a chunk journal; review persistent samples",
            })
        if states.get("failed", 0):
            alerts.append({
                "code": "failed_upload_jobs", "severity": "warning",
                "count": states["failed"],
                "message": "Failed upload jobs require explicit operator retry or cancellation",
            })
        if pending:
            alerts.append({
                "code": "stale_pending_uploads", "severity": "warning",
                "count": len(pending),
                "message": "Expired PENDING writers need dry-run recovery review",
            })
        if completing:
            alerts.append({
                "code": "stale_multipart_completions", "severity": "warning",
                "count": len(completing),
                "message": "Stale multipart completion leases need dry-run review",
            })
        truncated = len(pending) >= 1000 or len(completing) >= 1000
        if truncated:
            alerts.append({
                "code": "sample_limit_reached", "severity": "critical",
                "count": None,
                "message": "Recovery sample limit reached; inventory may be incomplete",
            })
        return {
            "job_states": states,
            "alerts": alerts,
            "unresolved_alert_count": len(alerts),
            "stale_pending_sample_count": len(pending),
            "stale_multipart_sample_count": len(completing),
            "unjournaled_bot_ack_count": unjournaled_bot_acks,
            "unresolved_bot_send_count": unresolved_bot_sends,
            "bot_explicit_429_total": explicit_bot_429_count,
            "samples_truncated": truncated,
            "telegram_messages_retained": gc.total_telegram_messages,
            "live_object_heads": gc.live_object_heads,
            "copy_aliases": gc.copy_aliases,
            "physical_deletion_enabled": False,
        }
    finally:
        conn.close()


@app.get("/api/v1/admin/storage/bot-unresolved", tags=["Storage jobs"],
         summary="Inspect unresolved Bot send intents without modifying Telegram")
def list_unresolved_bot_sends(
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_auth),
):
    # Operator-only diagnostic: return immutable source identity for
    # independent channel proof. Never accept unverified receipt IDs or
    # perform a retry/deletion as a side effect of inspecting this list.
    from teledrive.storage.bot_api_adapter import BotFileIndex

    conn = db_connect()
    try:
        index = BotFileIndex(conn)
        return {
            "total": index.unresolved_count(),
            "intents": index.unresolved_details(limit=limit),
            "message": (
                "Possible remotely accepted Bot sends. Independently verify "
                "original channel message ID, Bot file ID and full Telegram "
                "bytes before recording any recovery receipt. No automatic retry."
            ),
        }
    finally:
        conn.close()


@app.get("/api/v1/admin/storage/jobs", tags=["Storage jobs"],
         summary="Inspect durable local-source upload jobs (no worker tokens)")
def list_storage_upload_jobs(
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_auth),
):
    conn = db_connect()
    try:
        return {"jobs": StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
                .list_jobs(limit=limit)}
    finally:
        conn.close()


@app.post("/api/v1/admin/storage/jobs", tags=["Storage jobs"],
          status_code=201, summary="Queue an allowed-root file for Telegram upload")
def create_storage_upload_job(
    request: Request, payload: StorageUploadJobRequest,
    _: None = Depends(require_auth),
):
    _same_origin(request)
    root = _storage_job_root()
    conn = db_connect()
    try:
        manager = StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
        try:
            job_id = manager.enqueue(
                payload.bucket, payload.key, Path(payload.source_path),
                allowed_root=root,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (UploadJobError, ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"id": job_id, "state": "queued"}
    finally:
        conn.close()


@app.post("/api/v1/admin/storage/jobs/import-legacy",
          tags=["Storage jobs"], summary="Preview or queue legacy folder files without uploading")
def import_legacy_folder_jobs(
    request: Request, payload: LegacyFolderImportRequest,
    _: None = Depends(require_auth),
):
    _same_origin(request)
    from teledrive.storage.legacy_bridge import enqueue_legacy_folder
    approved_root = _storage_job_root()
    cfg = effective_config()
    try:
        source_root = Path(cfg.source_dir).resolve(strict=True)
    except OSError as exc:
        raise HTTPException(503, "Legacy source folder unavailable") from exc
    if not source_root.is_relative_to(approved_root):
        raise HTTPException(403, "Legacy source is outside the configured job root")
    conn = db_connect()
    try:
        now = int(time.time())
        holder = "legacy-bridge-" + os.urandom(16).hex()
        if not conn.acquire_lease(
            uploader.LEASE_NAME, holder, now, uploader.LEASE_TTL_SECONDS
        ):
            raise HTTPException(409, "Legacy uploader currently holds a lease")
        try:
            last_renew = time.monotonic()

            def heartbeat():
                nonlocal last_renew
                if time.monotonic() - last_renew < 10:
                    return
                if not conn.renew_lease(
                    uploader.LEASE_NAME, holder,
                    int(time.time()), uploader.LEASE_TTL_SECONDS
                ):
                    raise RuntimeError("Legacy uploader lease lost during import")
                last_renew = time.monotonic()

            manager = StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
            try:
                report = enqueue_legacy_folder(
                    cfg, manager, bucket=payload.bucket, prefix=payload.prefix,
                    max_files=payload.max_files,
                    dry_run=not payload.execute, lease_heartbeat=heartbeat,
                )
            except FileNotFoundError as exc:
                raise HTTPException(404, "Destination object bucket not found") from exc
            except (UploadJobError, ValueError, OSError) as exc:
                raise HTTPException(422, str(exc)) from exc
            return {
                "dry_run": not payload.execute,
                "queued": report.queued if payload.execute else 0,
                "would_queue": report.queued,
                "already_queued": report.already_queued,
                "excluded": report.excluded,
                "job_ids": list(report.job_ids) if payload.execute else [],
                "uploads_started": 0,
            }
        finally:
            conn.release_lease(uploader.LEASE_NAME, holder)
    finally:
        conn.close()


@app.post("/api/v1/admin/storage/jobs/run-next", tags=["Storage jobs"],
          summary="Process one queued upload with SQL lease, checksums and CAS")
async def run_next_storage_upload_job(
    request: Request, _: None = Depends(require_auth),
):
    _same_origin(request)
    if os.environ.get("TELEDRIVE_JOB_LEGACY_AUTO_BUCKET", "").strip():
        raise HTTPException(409, "Manual job execution is disabled during leased legacy cutover")
    root = _storage_job_root()
    conn = db_connect()
    try:
        manager = StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
        transport = storage_bot_transport(conn)
        scratch = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
        _secure_mkdir(scratch)
        try:
            result = await manager.run_next(
                transport, allowed_root=root, scratch_directory=scratch,
                chunk_size=BOT_RECOMMENDED_CHUNK_SIZE,
            )
        except (UploadJobError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, "Telegram upload failed; job retained for retry") from exc
        return {"job": result}
    finally:
        conn.close()


@app.post("/api/v1/admin/storage/jobs/{job_id}/retry", tags=["Storage jobs"],
          summary="Explicitly retry a failed job; never remove Telegram messages")
def retry_storage_upload_job(
    job_id: str, request: Request, _: None = Depends(require_auth),
):
    _same_origin(request)
    conn = db_connect()
    try:
        manager = StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
        if not manager.retry(job_id):
            raise HTTPException(409, "Job is not failed or does not exist")
        return {"id": job_id, "state": "queued"}
    finally:
        conn.close()


@app.post("/api/v1/admin/storage/jobs/{job_id}/cancel", tags=["Storage jobs"],
          summary="Cancel only inactive jobs; preserve all Telegram media")
def cancel_storage_upload_job(
    job_id: str, request: Request, _: None = Depends(require_auth),
):
    _same_origin(request)
    conn = db_connect()
    try:
        manager = StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
        if not manager.cancel(job_id):
            raise HTTPException(409, "Job is active, completed, cancelled or does not exist")
        return {"id": job_id, "state": "cancelled"}
    finally:
        conn.close()


@app.get("/api/v1/admin/storage/pending-stale", tags=["Storage recovery"],
         summary="Preview aged PENDING upload leases; no Telegram deletion")
def list_stale_pending_objects(
    min_age_seconds: int = Query(default=3600, ge=60, le=30 * 86400),
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_auth),
):
    conn = db_connect()
    try:
        items = ManifestStore(conn).stale_pending(
            now=int(time.time()), min_age_seconds=min_age_seconds, limit=limit
        )
        return {"uploads": items, "dry_run": True, "physical_deletion_enabled": False}
    finally:
        conn.close()


@app.post("/api/v1/admin/storage/pending-stale/{object_id}/abandon",
          tags=["Storage recovery"],
          summary="Explicitly fence an aged PENDING writer and retain Telegram journal")
def abandon_stale_pending_object(
    object_id: str, request: Request,
    payload: PendingUploadRecoveryRequest,
    _: None = Depends(require_auth),
):
    _same_origin(request)
    conn = db_connect()
    try:
        manifests = ManifestStore(conn)
        now = int(time.time())
        # A preview never mutates the writer's lease, even after a restart.
        if not payload.execute:
            matches = manifests.stale_pending(
                now=now, min_age_seconds=payload.min_age_seconds, limit=1000
            )
            return {"object_id": object_id,
                    "eligible": any(item["id"] == object_id for item in matches),
                    "abandoned": False, "dry_run": True}
        # Only this object can be transitioned. The SQL conditional update
        # ensures a heartbeat, completed object or another admin wins safely.
        affected = manifests.abandon_stale_pending(
            now=now, min_age_seconds=payload.min_age_seconds,
            limit=1, object_id=object_id,
        )
        return {"object_id": object_id, "eligible": bool(affected),
                "abandoned": bool(affected), "dry_run": False}
    finally:
        conn.close()


@app.get("/api/v1/admin/storage/channel-history", tags=["Storage history"],
         summary="Bounded read-only user-MTProto channel history (unverified candidates)")
async def inspect_user_channel_history(
    limit: int = Query(default=100, ge=1, le=1000),
    before_message_id: int | None = Query(default=None, ge=1),
    include_external_markers: bool = Query(default=False),
    _: None = Depends(require_auth),
):
    """Never download media, change catalog mappings or delete channel posts.

    Other senders' markers are NOT trusted ownership/manifest evidence.
    The endpoint deliberately only runs with the already-connected USER
    transport; a Bot token cannot enumerate arbitrary history.
    """
    if transport_mode() != "telethon":
        raise HTTPException(503, "User MTProto history requires telethon storage mode")
    try:
        runtime = mtproto_storage_runtime
        runtime.require()
        target = runtime.transport.accounts[runtime.account][1]
        return await inventory_channel(
            runtime.client, target, account=runtime.account,
            scan_limit=limit, before_message_id=before_message_id,
            include_external_markers=include_external_markers,
        )
    except (ValueError, RuntimeError, OSError):
        raise HTTPException(503, "Authorized channel history inspection unavailable") from None


@app.get("/api/v1/admin/s3-multipart/stale", tags=["API keys"],
         summary="Preview expired multipart completion leases (never deletes chunks)")
def list_stale_s3_completions(
    min_age_seconds: int = Query(default=3600, ge=60, le=30 * 86400),
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_auth),
):
    conn = db_connect()
    try:
        mpu = MultipartStore(BucketCatalog(ManifestStore(conn)))
        return {"uploads": mpu.stale_completions(
            min_age_seconds=min_age_seconds, limit=limit
        )}
    finally:
        conn.close()


@app.post("/api/v1/admin/s3-multipart/{upload_id}/recover", tags=["API keys"],
          summary="Dry-run or explicitly recover an expired multipart completion lease")
def recover_stale_s3_completion(
    request: Request, upload_id: str, payload: S3CompletionRecoveryRequest,
    _: None = Depends(require_auth),
):
    _same_origin(request)
    conn = db_connect()
    try:
        mpu = MultipartStore(BucketCatalog(ManifestStore(conn)))
        eligible = mpu.recover_stale_completion(
            upload_id, dry_run=not payload.execute,
            min_age_seconds=payload.min_age_seconds,
        )
        return {"upload_id": upload_id, "eligible": eligible,
                "recovered": bool(eligible and payload.execute),
                "dry_run": not payload.execute}
    finally:
        conn.close()


@app.get("/api/v1/admin/s3-credentials", tags=["API keys"],
         summary="List S3 access key metadata (secrets never returned)")
def list_s3_credentials(_: None = Depends(require_auth)):
    conn = db_connect()
    try:
        try:
            return {"credentials": S3CredentialStore(conn, load_master_key()).list()}
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    finally:
        conn.close()


@app.post("/api/v1/admin/s3-credentials", tags=["API keys"], status_code=201,
          summary="Issue encrypted S3 access credentials (secret displayed once)")
def issue_s3_credentials(request: Request, payload: S3KeyCreateRequest,
                         _: None = Depends(require_auth)):
    _same_origin(request)
    conn = db_connect()
    try:
        try:
            key = S3CredentialStore(conn, load_master_key()).create(
                payload.name, payload.permissions, expires_at=payload.expires_at
            )
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"id": key.id, "access_key": key.access_key,
                "secret_key": key.secret_key, "permissions": key.permissions}
    finally:
        conn.close()


@app.delete("/api/v1/admin/s3-credentials/{identifier}", tags=["API keys"],
            summary="Revoke an S3 access key immediately")
def revoke_s3_credentials(request: Request, identifier: str,
                          _: None = Depends(require_auth)):
    _same_origin(request)
    conn = db_connect()
    try:
        try:
            changed = S3CredentialStore(conn, load_master_key()).revoke(identifier)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if not changed:
            raise HTTPException(404, "S3 key not found or already revoked")
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/buckets", tags=["Object catalog"], summary="List Telegram-backed storage buckets")
def list_storage_buckets(_: None = Depends(require_api_scope("buckets:read"))):
    conn = db_connect()
    try:
        return {"buckets": BucketCatalog(ManifestStore(conn)).list_buckets()}
    finally:
        conn.close()


@app.post("/api/v1/buckets", tags=["Object catalog"], status_code=201, summary="Create a bucket")
def create_storage_bucket(payload: BucketCreateRequest,
                          _: None = Depends(require_api_scope("buckets:manage"))):
    conn = db_connect()
    try:
        try:
            BucketCatalog(ManifestStore(conn)).create_bucket(payload.name, int(time.time()))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"name": payload.name}
    finally:
        conn.close()


@app.delete("/api/v1/buckets/{bucket}", tags=["Object catalog"], summary="Remove a physically empty bucket")
def remove_storage_bucket(bucket: str, _: None = Depends(require_api_scope("buckets:manage"))):
    conn = db_connect()
    try:
        try:
            BucketCatalog(ManifestStore(conn)).delete_bucket(bucket)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/buckets/{bucket}/versions", tags=["Object catalog"],
         summary="List proven published immutable versions of a key")
def list_published_object_versions(
    bucket: str, key: str = Query(..., min_length=1, max_length=1024),
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_api_scope("objects:read")),
):
    conn = db_connect()
    try:
        catalog = BucketCatalog(ManifestStore(conn))
        if bucket not in catalog.list_buckets():
            raise HTTPException(404, "Bucket not found")
        return {"bucket": bucket, "key": key,
                "versions": catalog.list_versions(bucket, key, limit=limit)}
    finally:
        conn.close()


@app.get("/api/v1/buckets/{bucket}/objects", tags=["Object catalog"], summary="List current object metadata")
def list_current_objects(bucket: str,
                         prefix: str = Query("", max_length=1024),
                         after: str | None = Query(None, max_length=1024),
                         limit: int = Query(100, ge=1, le=1000),
                         _: None = Depends(require_api_scope("objects:read"))):
    conn = db_connect()
    try:
        items = BucketCatalog(ManifestStore(conn)).list_objects(bucket, prefix=prefix, after=after, limit=limit)
        return {"objects": [
            {"id": x.item.id, "key": x.item.key, "bucket": x.item.bucket,
             "size": x.item.size, "sha256": x.item.sha256,
             "created_at": x.item.created_at, "generation": x.generation}
            for x in items
        ]}
    finally:
        conn.close()


@app.delete("/api/v1/buckets/{bucket}/objects", tags=["Object catalog"], summary="Unlink a current object (Telegram data retained)")
def unlink_current_object(bucket: str, key: str = Query(..., min_length=1, max_length=1024),
                          generation: int | None = Query(None, ge=1),
                          _: None = Depends(require_api_scope("objects:delete"))):
    conn = db_connect()
    try:
        try:
            deleted = BucketCatalog(ManifestStore(conn)).remove_current(
                bucket, key, expected_generation=generation
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "unlinked": deleted}
    finally:
        conn.close()


@app.put("/api/v1/buckets/{bucket}/content", tags=["Uploads"],
         summary="Upload raw binary content to Telegram (synchronous commit)", status_code=201)
async def put_object_content(
    bucket: str, request: Request, key: str = Query(..., min_length=1, max_length=1024),
    generation: int | None = Query(None, ge=0),
    _: None = Depends(require_api_scope("objects:write")),
):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/octet-stream":
        raise HTTPException(415, "Content-Type must be application/octet-stream")
    max_size = int(os.environ.get("TELEDRIVE_REST_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
    if max_size < 1:
        raise HTTPException(503, "REST uploads are disabled by policy")
    stage_root = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
    _secure_mkdir(stage_root)
    conn = db_connect()
    staged = None
    try:
        manifests = ManifestStore(conn)
        catalog = BucketCatalog(manifests)
        if bucket not in catalog.list_buckets():
            raise HTTPException(404, "Bucket not found")
        if generation is not None:
            try:
                current_generation = catalog.get(bucket, key).generation
            except FileNotFoundError:
                current_generation = 0
            if current_generation != generation:
                # Reject stale conditional writes BEFORE staging/uploading any
                # bytes to Telegram. Recheck atomically at final publication.
                raise HTTPException(409, "Object generation precondition failed")
        transport = storage_bot_transport(conn)
        # Verify Telegram access BEFORE consuming a potentially large body.
        await transport.verify_target()
        fd, pathname = tempfile.mkstemp(dir=stage_root, prefix="api-upload-")
        staged = Path(pathname)
        transferred = 0
        with os.fdopen(fd, "wb") as sink:
            async for data in request.stream():
                transferred += len(data)
                if transferred > max_size:
                    raise HTTPException(413, "Upload exceeds configured limit")
                sink.write(data)
            sink.flush()
            os.fsync(sink.fileno())
        engine = TelegramObjectStorage(manifests, transport,
                                       chunk_size=BOT_RECOMMENDED_CHUNK_SIZE,
                                       scratch_directory=stage_root)
        result = await engine.put_file(bucket, key, staged)
        try:
            head = catalog.publish(result.id, now=int(time.time()),
                                   expected_generation=generation)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": result.id, "bucket": bucket, "key": key,
                "size": result.size, "sha256": result.sha256,
                "generation": head.generation}
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        conn.close()


@app.head("/api/v1/buckets/{bucket}/content", tags=["Uploads"],
          summary="Read object headers without downloading Telegram bytes")
def head_object_content(
    bucket: str, key: str = Query(..., min_length=1, max_length=1024),
    _: None = Depends(require_api_scope("objects:read")),
):
    conn = db_connect()
    try:
        try:
            item = BucketCatalog(ManifestStore(conn)).get(bucket, key).item
        except FileNotFoundError as exc:
            raise HTTPException(404, "Object not found") from exc
        return Response(status_code=200, headers={
            "ETag": '"' + item.sha256 + '"',
            "Content-Length": str(item.size),
            "Accept-Ranges": "bytes",
            "X-TeleDrive-Object-ID": item.id,
            "Content-Type": "application/octet-stream",
        })
    finally:
        conn.close()


@app.get("/api/v1/buckets/{bucket}/content", tags=["Uploads"],
         summary="Stream verified object bytes from Telegram; supports HTTP Range")
async def get_object_content(
    bucket: str, request: Request, key: str = Query(..., min_length=1, max_length=1024),
    _: None = Depends(require_api_scope("objects:read")),
):
    conn = db_connect()
    try:
        manifests = ManifestStore(conn)
        catalog = BucketCatalog(manifests)
        try:
            item = catalog.get(bucket, key).item
        except FileNotFoundError as exc:
            raise HTTPException(404, "Object not found") from exc
        start, end, partial = _byte_range(request.headers.get("range"), item.size)
        transport = storage_bot_transport(conn)
        root = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
        _secure_mkdir(root)
        engine = TelegramObjectStorage(manifests, transport,
                                       chunk_size=BOT_RECOMMENDED_CHUNK_SIZE,
                                       scratch_directory=root)
    except BaseException:
        conn.close()
        raise

    async def stream():
        try:
            async for piece in engine.iter_bytes(item.id, start=start, end=end):
                yield piece
        finally:
            conn.close()

    headers = {"ETag": '"' + item.sha256 + '"', "Accept-Ranges": "bytes",
               "Content-Length": str(end-start), "X-TeleDrive-Object-ID": item.id}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end-1}/{item.size}"
    return StreamingResponse(stream(), status_code=206 if partial else 200,
                             media_type="application/octet-stream", headers=headers)


@app.get("/api/health", tags=["System"], response_model=OKResponse, summary="Liveness health check (unauthenticated)")
def health():
    return {"ok": True}
