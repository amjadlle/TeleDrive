"""Central service settings, loaded from config/config.yaml only.

The owner policy: everything Telethon-only and config-driven. No service
settings come from environment variables except TZ and the config-file
path (TELEDRIVE_CONFIG). The WebUI admin config endpoints read and write
the same file, so UI changes persist to disk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any


# Owner policy: the service binds every in-container interface;
# the host-side port mapping stays loopback.  # nosec B104
DEFAULT_BIND_ADDRESS = "0.0.0.0"  # nosec B104


@dataclass
class ServiceSettings:
    web_user: str = "admin"
    web_password: str = ""
    web_password_file: str = ""
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")
    allow_insecure: bool = False
    web_port: int = 8080
    s3_port: int = 9000
    bind_address: str = DEFAULT_BIND_ADDRESS
    rest_max_upload_bytes: int = 2 * 1024**3
    max_request_bytes: int = 32 * 1024**2
    storage_scratch_dir: str = "/data/tmp"
    job_source_root: str = "/uploads"
    job_auto_run: bool = True
    allowed_sqlite_roots: tuple[str, ...] = ("/data",)
    auth_failure_limit: int = 5
    auth_failure_window_seconds: int = 300
    ssl_certfile: str = ""
    ssl_keyfile: str = ""
    db_extra_packages: tuple[str, ...] = ()
    auth_dir: str = "/data/auth"
    config_path: str = "config/config.yaml"
    s3_master_key_file: str = "/config/s3-master.key"
    allowed_source_roots: str = "/uploads"
    allowed_session_roots: str = "/data/sessions"
    allowed_log_roots: str = "/data"
    split_dir: str = "/data/tmp"
    split_min_free_bytes: int = 512 * 1024 * 1024
    lock_dir: str = "/data/tmp"


def _section(cfg: Any, name: str) -> dict:
    section = getattr(cfg, name, None)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f"Invalid {name} configuration section")
    return section


def _hosts(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    if isinstance(value, str):
        entries = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        entries = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise ValueError("Invalid allowed-hosts configuration")
    if not entries:
        return default
    return tuple(entries)


def _int(value: Any, default: int, name: str, *, minimum: int = 0) -> int:
    if value is None or value == "":
        return default
    parsed = int(value)
    if parsed < minimum:
        raise ValueError(f"Invalid {name} configuration")
    return parsed


def load_settings(cfg: Any, config_path: str) -> ServiceSettings:
    """Build service settings from the parsed deployment config.

    `cfg` is the AppConfig produced by teledrive.uploader.load_config; its
    extra web/jobs/storage sections hold the service configuration.
    """
    web = _section(cfg, "web")
    jobs = _section(cfg, "jobs")
    storage = _section(cfg, "storage")
    state = _section(cfg, "state")

    paths = _section(cfg, "paths")
    extra_packages = state.get("db_extra_packages", ())
    if not extra_packages:
        packages = ()
    elif isinstance(extra_packages, str):
        packages = tuple(
            part.strip() for part in extra_packages.split(",") if part.strip()
        )
    elif isinstance(extra_packages, (list, tuple)):
        packages = tuple(str(part).strip() for part in extra_packages
                         if str(part).strip())
    else:
        raise ValueError("Invalid db_extra_packages configuration")

    return ServiceSettings(
        web_user=str(_cfg_or_env("user", "TELEDRIVE_WEB_USER", "admin")),
        web_password=str(_cfg_or_env("password", "TELEDRIVE_WEB_PASSWORD", "")),
        web_password_file=str(_cfg_or_env("password_file", "TELEDRIVE_WEB_PASSWORD_FILE", "")),
        allowed_hosts=_hosts(_cfg_or_env("allowed_hosts", "TELEDRIVE_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"),
                             ("localhost", "127.0.0.1", "[::1]")),
        allow_insecure=bool(
            _cfg_or_env("allow_insecure", "TELEDRIVE_ALLOW_INSECURE", False)
            in (True, "1", "true", "yes", "True")
        ),
        web_port=_int(_cfg_or_env("port", "TELEDRIVE_WEB_PORT", 8080), 8080,
                      "web port", minimum=1),
        s3_port=_int(_cfg_or_env("s3_port", "TELEDRIVE_S3_PORT", 9000), 9000,
                     "s3 port", minimum=1),
        bind_address=str(_cfg_or_env("bind_address", "TELEDRIVE_BIND_ADDRESS",
                                     DEFAULT_BIND_ADDRESS)),
        rest_max_upload_bytes=_int(
            _cfg_or_env("rest_max_upload_bytes", "TELEDRIVE_REST_MAX_UPLOAD_BYTES", 2 * 1024**3),
            2 * 1024**3, "rest_max_upload_bytes", minimum=1),
        max_request_bytes=_int(
            _cfg_or_env("max_request_bytes", "TELEDRIVE_MAX_REQUEST_BYTES", 32 * 1024**2),
            32 * 1024**2, "max_request_bytes", minimum=1),
        storage_scratch_dir=str(
            storage.get("scratch_dir")
            or os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp")),
        job_source_root=str(
            jobs.get("source_root")
            or os.environ.get("TELEDRIVE_JOB_SOURCE_ROOT", "/uploads")),
        job_auto_run=bool(jobs.get("auto_run",
                          os.environ.get("TELEDRIVE_JOB_AUTO_RUN", "1") == "1")),
        allowed_sqlite_roots=_hosts(
            state.get("allowed_sqlite_roots")
            or os.environ.get("TELEDRIVE_ALLOWED_SQLITE_ROOTS", "/data"),
            ("/data",)),
        auth_failure_limit=_int(
            _cfg_or_env("auth_failure_limit", "TELEDRIVE_AUTH_FAILURE_LIMIT", 5),
            5, "auth_failure_limit", minimum=1),
        auth_failure_window_seconds=_int(
            _cfg_or_env("auth_failure_window_seconds",
                        "TELEDRIVE_AUTH_FAILURE_WINDOW_SECONDS", 300),
            300, "auth_failure_window_seconds", minimum=1),
        ssl_certfile=str(_cfg_or_env("ssl_certfile", "TELEDRIVE_SSL_CERTFILE", "")),
        ssl_keyfile=str(_cfg_or_env("ssl_keyfile", "TELEDRIVE_SSL_KEYFILE", "")),
        db_extra_packages=packages,
        auth_dir=str(_cfg_or_env("auth_dir", "TELEDRIVE_AUTH_DIR", "/data/auth")),
        config_path=config_path,
        s3_master_key_file=str(
            storage.get("s3_master_key_file")
            or os.environ.get("TELEDRIVE_S3_MASTER_KEY_FILE", "/config/s3-master.key")),
        allowed_source_roots=str(
            paths.get("source_roots")
            or os.environ.get("TELEDRIVE_ALLOWED_SOURCE_ROOTS", "/uploads")),
        allowed_session_roots=str(
            paths.get("session_roots")
            or os.environ.get("TELEDRIVE_ALLOWED_SESSION_ROOTS", "/data/sessions")),
        allowed_log_roots=str(
            paths.get("log_roots")
            or os.environ.get("TELEDRIVE_ALLOWED_LOG_ROOTS", "/data")),
        split_dir=str(
            paths.get("split_dir")
            or os.environ.get("TELEDRIVE_SPLIT_DIR", "/data/tmp")),
        split_min_free_bytes=_int(
            paths.get("split_min_free_bytes")
            or os.environ.get("TELEDRIVE_SPLIT_MIN_FREE_BYTES", 512 * 1024 * 1024),
            512 * 1024 * 1024, "split_min_free_bytes", minimum=1),
        lock_dir=str(
            paths.get("lock_dir")
            or os.environ.get("TELEDRIVE_LOCK_DIR", "/data/tmp")),
    )


def install_db_extra_packages(settings: ServiceSettings,
                              target_dir: str) -> None:
    """Install configured extra DB drivers into a writable runtime dir.

    The container root filesystem is read-only, so drivers go under the
    data volume and are prepended to sys.path before any database URL is
    opened. Failures are loud: a configured driver must import.
    """
    if not settings.db_extra_packages:
        return
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    import subprocess
    import sys

    command = [
        sys.executable, "-m", "pip", "install", "--no-cache-dir",
        "--target", str(target),
        *settings.db_extra_packages,
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "Configured DB extra packages could not be installed: "
            + (completed.stderr or completed.stdout)[-800:]
        )
    sys.path.insert(0, str(target))
