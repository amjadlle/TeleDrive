from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import (
    BigInteger, Column, Float, Index, Integer, MetaData, String, Table, Text,
    and_, create_engine, delete, func, insert, inspect, or_, select, update,
)
from sqlalchemy.engine import Connection, Engine, Row
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError, NoSuchModuleError
from sqlalchemy.schema import CreateColumn

metadata = MetaData()

files = Table(
    "files", metadata,
    Column("path", String(700), primary_key=True),
    Column("size", BigInteger, nullable=False),
    Column("mtime", Float, nullable=False),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("tg_message_id", BigInteger),
    Column("tg_account", String(255)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("first_seen_ts", BigInteger, nullable=False),
    Column("last_update_ts", BigInteger, nullable=False),
    Column("uploaded_ts", BigInteger),
    Column("claim_token", String(255)),
    Column("claim_ts", BigInteger),
)
Index("idx_files_status", files.c.status)
Index("idx_files_tg_account", files.c.tg_account)

account_status = Table(
    "account_status", metadata,
    Column("name", String(255), primary_key=True),
    Column("state", String(32), nullable=False, server_default="unknown"),
    Column("last_error", Text),
    Column("cooldown_until", BigInteger),
    Column("last_success_ts", BigInteger),
    Column("last_update_ts", BigInteger, nullable=False),
)

leases = Table(
    "leases", metadata,
    Column("name", String(128), primary_key=True),
    Column("holder", String(255), nullable=False),
    Column("lease_until", BigInteger, nullable=False),
    Column("last_update_ts", BigInteger, nullable=False),
)


class DBRow:
    def __init__(self, row: Row[Any]):
        self._mapping = dict(row._mapping)
        self._values = tuple(self._mapping.values())

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._mapping[key]

    def as_dict(self) -> dict[str, Any]:
        return dict(self._mapping)


def normalize_database_url(value: str, *, config_dir: Path | None = None) -> str:
    """Accept any synchronous SQLAlchemy URL; local paths become SQLite URLs."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Database URL/path must not be empty")

    if "://" not in raw:
        path = Path(raw).expanduser()
        if not path.is_absolute() and config_dir is not None:
            path = config_dir / path
        return f"sqlite:///{path.resolve().as_posix()}"

    # Convenience aliases only. Explicit dialect+driver URLs pass through unchanged.
    if raw.startswith("postgres://"):
        raw = "postgresql+psycopg://" + raw[len("postgres://"):]
    elif raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    elif raw.startswith("mysql://"):
        raw = "mysql+pymysql://" + raw[len("mysql://"):]

    if raw.startswith("sqlite:///") and not raw.startswith("sqlite:////") and raw != "sqlite:///:memory:" and config_dir:
        rel = raw[len("sqlite:///"):]
        return f"sqlite:///{(config_dir / rel).expanduser().resolve().as_posix()}"
    return raw


def sqlite_database_path(url: str) -> str | None:
    """Return the filesystem path for a SQLite URL, otherwise None."""
    try:
        parsed = make_url(normalize_database_url(url))
    except Exception:
        return None
    if parsed.get_backend_name() != "sqlite":
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return str(Path(database).expanduser().resolve())


def redact_database_url(url: str) -> str:
    """Render a URL without userinfo passwords or sensitive query values."""
    try:
        rendered = make_url(normalize_database_url(url)).render_as_string(hide_password=True)
        parts = urlsplit(rendered)
        sensitive = {"password", "passwd", "pwd", "token", "access_token", "secret", "api_key", "apikey"}
        query = [
            (key, "***" if key.lower() in sensitive else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except Exception:
        return "<invalid database URL>"


@dataclass
class Database:
    url: str
    engine: Engine
    connection: Connection

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            self.engine.dispose()

    def commit(self) -> None:
        self.connection.commit()

    def file_signature(self, path: str) -> DBRow | None:
        row = self.connection.execute(
            select(files.c.size, files.c.mtime, files.c.status).where(files.c.path == path)
        ).first()
        return DBRow(row) if row else None

    def insert_pending(self, path: str, size: int, mtime: float, now: int) -> None:
        self.connection.execute(insert(files).values(
            path=path, size=size, mtime=mtime, status="pending",
            first_seen_ts=now, last_update_ts=now,
        ))

    def requeue_changed(self, path: str, size: int, mtime: float, now: int) -> None:
        self.connection.execute(update(files).where(files.c.path == path).values(
            size=size, mtime=mtime, status="pending", tg_message_id=None,
            tg_account=None, uploaded_ts=None, last_error=None,
            claim_token=None, claim_ts=None, last_update_ts=now,
        ))

    def uploaded_between(self, start_ts: int, end_ts: int) -> int:
        return int(self.connection.execute(
            select(func.count()).select_from(files).where(
                files.c.status == "uploaded",
                files.c.uploaded_ts >= start_ts,
                files.c.uploaded_ts < end_ts,
            )
        ).scalar_one() or 0)

    def pending(self, limit: int, now: int, stale_after_seconds: int) -> list[DBRow]:
        stale_before = now - stale_after_seconds
        rows = self.connection.execute(
            select(files.c.path, files.c.size, files.c.mtime, files.c.attempts)
            .where(
                or_(
                    files.c.status.in_(["pending", "failed"]),
                    and_(
                        files.c.status == "uploading",
                        or_(files.c.claim_ts.is_(None), files.c.claim_ts < stale_before),
                    ),
                )
            )
            .order_by(files.c.first_seen_ts.asc()).limit(limit)
        ).all()
        return [DBRow(row) for row in rows]

    def claim_file(self, path: str, claim_token: str, now: int, stale_after_seconds: int) -> bool:
        stale_before = now - stale_after_seconds
        self.connection.execute(
            update(files)
            .where(
                files.c.path == path,
                or_(
                    files.c.status.in_(["pending", "failed"]),
                    and_(
                        files.c.status == "uploading",
                        or_(files.c.claim_ts.is_(None), files.c.claim_ts < stale_before),
                    ),
                ),
            )
            .values(
                status="uploading",
                claim_token=claim_token,
                claim_ts=now,
                last_update_ts=now,
            )
        )
        self.connection.commit()
        row = self.connection.execute(
            select(files.c.status, files.c.claim_token).where(files.c.path == path)
        ).first()
        return bool(row and row[0] == "uploading" and row[1] == claim_token)

    def mark_failed(self, path: str, error: str, attempts_inc: int, now: int) -> None:
        self.connection.execute(update(files).where(files.c.path == path).values(
            status="failed", attempts=files.c.attempts + attempts_inc,
            last_error=error[:1000], claim_token=None, claim_ts=None, last_update_ts=now,
        ))

    def mark_uploaded(self, path: str, message_id: int | None, account_name: str, now: int) -> None:
        self.connection.execute(update(files).where(files.c.path == path).values(
            status="uploaded", tg_message_id=message_id, tg_account=account_name,
            attempts=files.c.attempts + 1, last_error=None,
            claim_token=None, claim_ts=None, uploaded_ts=now, last_update_ts=now,
        ))

    def account_state(self, name: str) -> DBRow | None:
        row = self.connection.execute(
            select(account_status).where(account_status.c.name == name)
        ).first()
        return DBRow(row) if row else None

    def set_account_state(self, name: str, state: str, *, last_error: str | None,
                          cooldown_until: int | None, last_success_ts: int | None, now: int) -> None:
        values = dict(
            state=state, last_error=last_error, cooldown_until=cooldown_until,
            last_success_ts=last_success_ts, last_update_ts=now,
        )
        result = self.connection.execute(
            update(account_status).where(account_status.c.name == name).values(**values)
        )
        if result.rowcount:
            return
        try:
            self.connection.execute(insert(account_status).values(name=name, **values))
        except IntegrityError:
            self.connection.rollback()
            self.connection.execute(
                update(account_status).where(account_status.c.name == name).values(**values)
            )

    def acquire_lease(self, name: str, holder: str, now: int, ttl_seconds: int) -> bool:
        lease_until = now + ttl_seconds
        result = self.connection.execute(
            update(leases)
            .where(
                leases.c.name == name,
                (leases.c.holder == holder) | (leases.c.lease_until < now),
            )
            .values(holder=holder, lease_until=lease_until, last_update_ts=now)
        )
        if result.rowcount:
            self.connection.commit()
            row = self.connection.execute(
                select(leases.c.holder, leases.c.lease_until).where(leases.c.name == name)
            ).first()
            return bool(row and row[0] == holder and int(row[1]) >= now)
        try:
            self.connection.execute(insert(leases).values(
                name=name, holder=holder, lease_until=lease_until, last_update_ts=now,
            ))
            self.connection.commit()
            return True
        except IntegrityError:
            self.connection.rollback()
            return False

    def renew_lease(self, name: str, holder: str, now: int, ttl_seconds: int) -> bool:
        result = self.connection.execute(
            update(leases)
            .where(leases.c.name == name, leases.c.holder == holder, leases.c.lease_until >= now)
            .values(lease_until=now + ttl_seconds, last_update_ts=now)
        )
        self.connection.commit()
        if not result.rowcount:
            self.connection.rollback()
            return False
        row = self.connection.execute(
            select(leases.c.holder, leases.c.lease_until).where(leases.c.name == name)
        ).first()
        return bool(row and row[0] == holder and int(row[1]) >= now)

    def release_lease(self, name: str, holder: str) -> None:
        self.connection.execute(
            delete(leases).where(leases.c.name == name, leases.c.holder == holder)
        )
        self.connection.commit()

    def dashboard_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            select(files.c.status, func.count()).group_by(files.c.status)
        ).all()
        return {str(row[0]): int(row[1]) for row in rows}

    def all_account_states(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(select(account_status).order_by(account_status.c.name)).all()
        return {str(row._mapping["name"]): dict(row._mapping) for row in rows}

    def uploaded_counts_by_account(self) -> dict[str, int]:
        label = func.coalesce(files.c.tg_account, "unknown").label("account")
        rows = self.connection.execute(
            select(label, func.count()).where(files.c.status == "uploaded").group_by(label)
        ).all()
        return {str(row[0]): int(row[1]) for row in rows}

    def list_files(self, kind: str, limit: int) -> list[dict[str, Any]]:
        condition = files.c.status.in_(["pending", "failed", "uploading"]) if kind == "queue" else files.c.status == "uploaded"
        rows = self.connection.execute(
            select(
                files.c.path, files.c.size, files.c.status, files.c.attempts,
                files.c.last_error, files.c.tg_account, files.c.tg_message_id,
                files.c.first_seen_ts, files.c.last_update_ts, files.c.uploaded_ts,
            ).where(condition).order_by(files.c.last_update_ts.desc()).limit(limit)
        ).all()
        return [dict(row._mapping) for row in rows]

    def clear_files(self) -> None:
        self.connection.execute(delete(files))


def connect_database(url_or_path: str, *, config_dir: Path | None = None) -> Database:
    url = normalize_database_url(url_or_path, config_dir=config_dir)
    connect_args: dict[str, Any] = {}

    if url.startswith("sqlite:"):
        connect_args["timeout"] = 30
        if url != "sqlite:///:memory:":
            db_path = url[len("sqlite:///"):]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    try:
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    except (NoSuchModuleError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "Database dialect/driver is not installed. Install the SQLAlchemy dialect/DBAPI "
            "required by the configured database_url."
        ) from exc

    metadata.create_all(engine)

    # Dialect-aware migrations for older TeleDrive databases.
    columns = {c["name"] for c in inspect(engine).get_columns("files")}
    migrations = [
        Column("tg_account", String(255)),
        Column("claim_token", String(255)),
        Column("claim_ts", BigInteger),
    ]
    missing = [column for column in migrations if column.name not in columns]
    if missing:
        preparer = engine.dialect.identifier_preparer
        table_name = preparer.quote("files")
        with engine.begin() as conn:
            for column in missing:
                rendered = str(CreateColumn(column).compile(dialect=engine.dialect))
                conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD {rendered}")

    connection = engine.connect()
    if engine.url.get_backend_name() == "sqlite":
        connection.exec_driver_sql("PRAGMA busy_timeout=30000")
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    return Database(url=url, engine=engine, connection=connection)
