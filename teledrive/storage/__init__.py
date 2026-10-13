"""Telegram-only object storage primitives.

The metadata database stores manifests only; object bytes live in Telegram.
This is an early storage-engine foundation, not yet an S3 gateway.
"""

from .engine import TelegramObjectStorage
from .manifest import ManifestStore, ChunkReference, StoredObject
from .recovery import RecoveryManager, CleanupReport, UnsafeCleanup

__all__ = ["TelegramObjectStorage", "ManifestStore", "ChunkReference", "StoredObject", "RecoveryManager", "CleanupReport", "UnsafeCleanup"]
