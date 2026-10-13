"""Encrypted, independently revocable S3 access credentials.

A dedicated Fernet master key is mandatory. DB backups alone never reveal
AWS secret keys. Losing the master key makes existing keys unusable.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import BigInteger, Boolean, Column, MetaData, String, Table, Text, insert, select, update

from teledrive.db import Database

_meta = MetaData()
credentials = Table(
    "storage_s3_credentials", _meta,
    Column("id", String(36), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("access_key", String(64), nullable=False, unique=True),
    Column("encrypted_secret", Text, nullable=False),
    Column("permissions", Text, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger),
    Column("revoked", Boolean, nullable=False),
)
PERMISSIONS = frozenset({
    "s3:ListAllMyBuckets", "s3:CreateBucket", "s3:DeleteBucket",
    "s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketLocation", "s3:GetObject", "s3:GetObjectAttributes", "s3:PutObject", "s3:DeleteObject",
    "s3:GetObjectTagging", "s3:PutObjectTagging", "s3:DeleteObjectTagging",
    "s3:GetBucketTagging", "s3:PutBucketTagging", "s3:DeleteBucketTagging",
    "s3:GetBucketCORS", "s3:PutBucketCORS",
})
DEFAULT_PERMISSIONS = tuple(sorted(PERMISSIONS))


def load_master_key() -> bytes:
    secret_file = os.environ.get("TELEDRIVE_S3_MASTER_KEY_FILE", "").strip()
    if secret_file:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(secret_file, flags)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(256).strip()
    else:
        data = os.environ.get("TELEDRIVE_S3_MASTER_KEY", "").encode("ascii").strip()
    try:
        Fernet(data)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Set a valid 32-byte Fernet TELEDRIVE_S3_MASTER_KEY") from exc
    return data


@dataclass(frozen=True)
class IssuedS3Credential:
    id: str
    access_key: str
    secret_key: str
    permissions: tuple[str, ...]


class S3CredentialStore:
    def __init__(self, database: Database, master_key: bytes):
        self.db = database
        self.cipher = Fernet(master_key)
        _meta.create_all(database.engine)

    def create(self, name: str, permissions: list[str], *,
               expires_at: int | None = None, now: int | None = None) -> IssuedS3Credential:
        now = int(time.time()) if now is None else now
        if not isinstance(name, str) or not (1 <= len(name.strip()) <= 120):
            raise ValueError("S3 credential requires a name (1–120 characters)")
        if not isinstance(permissions, list) or not permissions or any(
                p not in PERMISSIONS for p in permissions):
            raise ValueError("Unknown or empty S3 permission")
        if expires_at is not None and (expires_at <= now or expires_at > now + 10 * 365 * 86400):
            raise ValueError("Expiration must be in the future (up to 10 years)")
        from uuid import uuid4
        identifier = str(uuid4())
        access = "TD" + secrets.token_hex(12).upper()
        secret = secrets.token_urlsafe(40)
        perms = tuple(sorted(set(permissions)))
        self.db.connection.execute(insert(credentials).values(
            id=identifier, name=name.strip(), access_key=access,
            encrypted_secret=self.cipher.encrypt(secret.encode()).decode(),
            permissions=json.dumps(perms), created_at=now, expires_at=expires_at,
            revoked=False,
        ))
        self.db.commit()
        return IssuedS3Credential(identifier, access, secret, perms)

    def list(self) -> list[dict]:
        rows = self.db.connection.execute(select(
            credentials.c.id, credentials.c.name, credentials.c.access_key,
            credentials.c.permissions, credentials.c.created_at,
            credentials.c.expires_at, credentials.c.revoked,
        ).order_by(credentials.c.created_at.desc(), credentials.c.id)).mappings().all()
        return [{**dict(row), "permissions": json.loads(row["permissions"])} for row in rows]

    def resolve(self, access_key: str, permission: str, *,
                now: int | None = None) -> str | None:
        now = int(time.time()) if now is None else now
        if not isinstance(access_key, str) or len(access_key) > 64 or permission not in PERMISSIONS:
            return None
        row = self.db.connection.execute(select(
            credentials.c.encrypted_secret, credentials.c.permissions,
            credentials.c.revoked, credentials.c.expires_at,
        ).where(credentials.c.access_key == access_key)).first()
        if not row or row[2] or (row[3] is not None and row[3] <= now):
            return None
        if permission not in json.loads(row[1]):
            return None
        try:
            return self.cipher.decrypt(row[0].encode()).decode()
        except (InvalidToken, ValueError, UnicodeError):
            return None

    def revoke(self, identifier: str) -> bool:
        changed = self.db.connection.execute(update(credentials).where(
            credentials.c.id == identifier, credentials.c.revoked.is_(False)
        ).values(revoked=True))
        self.db.commit()
        return changed.rowcount == 1
