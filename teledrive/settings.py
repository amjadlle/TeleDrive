"""Central service settings, loaded from config/config.yaml only.

The owner policy: everything Telethon-only and config-driven. No service
settings come from environment variables except TZ and the config-file
path (TELEDRIVE_CONFIG). The WebUI admin config endpoints read and write
the same file, so UI changes persist to disk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_BIND_ADDRESS = "0.0.0.0"


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
    job_legacy_auto_bucket: str = ""
    job_legacy_auto_prefix: str = ""
    job_legacy_auto_limit: int = 100
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
    if isinstance(extra_packages, str):
        packages = tuple(
            part.strip() for part in extra_packages.split(",") if part.strip()
        )
    elif isinstance(extra_packages, list):
        packages = tuple(str(part).strip() for part in extra_packages
                         if str(part).strip())
    else:
        raise ValueError("Invalid db_extra_packages configuration")

    return ServiceSettings(
        web_user=str(web.get("user") or "admin"),
        web_password=str(web.get("password") or ""),
        web_password_file=str(web.get("password_file") or ""),
        allowed_hosts=_hosts(web.get("allowed_hosts"),
                             ("localhost", "127.0.0.1", "[::1]")),
        allow_insecure=bool(web.get("allow_insecure", False)),
        web_port=_int(web.get("port"), 8080, "web port", minimum=1),
        s3_port=_int(web.get("s3_port"), 9000, "s3 port", minimum=1),
        bind_address=str(web.get("bind_address") or DEFAULT_BIND_ADDRESS),
        rest_max_upload_bytes=_int(
            web.get("rest_max_upload_bytes"), 2 * 1024**3,
            "rest_max_upload_bytes", minimum=1),
        max_request_bytes=_int(
            web.get("max_request_bytes"), 32 * 1024**2,
            "max_request_bytes", minimum=1),
        storage_scratch_dir=str(
            storage.get("scratch_dir") or "/data/tmp"),
        job_source_root=str(jobs.get("source_root") or "/uploads"),
        job_auto_run=bool(jobs.get("auto_run", True)),
        job_legacy_auto_bucket=str(jobs.get("legacy_auto_bucket") or ""),
        job_legacy_auto_prefix=str(jobs.get("legacy_auto_prefix") or ""),
        job_legacy_auto_limit=_int(jobs.get("legacy_auto_limit"), 100,
                                   "legacy_auto_limit", minimum=1),
        allowed_sqlite_roots=_hosts(
            state.get("allowed_sqlite_roots"), ("/data",)),
        auth_failure_limit=_int(web.get("auth_failure_limit"), 5,
                                "auth_failure_limit", minimum=1),
        auth_failure_window_seconds=_int(
            web.get("auth_failure_window_seconds"), 300,
            "auth_failure_window_seconds", minimum=1),
        ssl_certfile=str(web.get("ssl_certfile") or ""),
        ssl_keyfile=str(web.get("ssl_keyfile") or ""),
        db_extra_packages=packages,
        auth_dir=str(web.get("auth_dir") or "/data/auth"),
        config_path=config_path,
        s3_master_key_file=str(
            storage.get("s3_master_key_file") or "/config/s3-master.key"),
        allowed_source_roots=str(
            paths.get("source_roots") or "/uploads"),
        allowed_session_roots=str(
            paths.get("session_roots") or "/data/sessions"),
        allowed_log_roots=str(paths.get("log_roots") or "/data"),
        split_dir=str(paths.get("split_dir") or "/data/tmp"),
        split_min_free_bytes=_int(
            paths.get("split_min_free_bytes"), 512 * 1024 * 1024,
            "split_min_free_bytes", minimum=1),
        lock_dir=str(paths.get("lock_dir") or "/data/tmp"),
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
