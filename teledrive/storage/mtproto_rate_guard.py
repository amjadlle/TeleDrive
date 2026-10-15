"""Shared MTProto send-slot, flood-wait cooldown and observation journal.

The Telethon USER transport is the only live payload path. Independent
workers sharing one SQL database must serialize sends per account/channel
and observe definitive FloodWaitError backoffs globally, exactly like the
retired Bot adapter did for HTTP 429. No method here sends or deletes
Telegram media; the guard is pure durable SQL fencing.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import (
    BigInteger, Column, Integer, MetaData, String, Table, case, func,
    insert, select, update,
)
from sqlalchemy.exc import IntegrityError as SQLIntegrityError
from sqlalchemy.exc import OperationalError as SQLOperationalError

_meta = MetaData()


# A process-local asyncio.Lock cannot coordinate independent USER workers
# sharing a Telegram channel. Reserve start slots in durable shared SQL.
# One committed UPDATE serializes competing processes on PostgreSQL/MySQL
# and SQLite. A slot is never proof that a Telegram send was accepted.
_mtproto_chat_slots = Table(
    "storage_mtproto_chat_send_slots", _meta,
    Column("account", String(255), primary_key=True),
    Column("chat_id", BigInteger, primary_key=True),
    Column("next_allowed_ms", BigInteger, nullable=False),
)


# Separate from the send-slot queue: a flood-wait can arrive *after*
# another worker already reserved a future slot. Pending workers must
# re-check the shared cooldown immediately before their MTProto send.
_mtproto_chat_cooldowns = Table(
    "storage_mtproto_chat_cooldowns", _meta,
    Column("account", String(255), primary_key=True),
    Column("chat_id", BigInteger, primary_key=True),
    Column("cooldown_until_ms", BigInteger, nullable=False),
)


# A durable, non-secret observation counter helps an operator distinguish
# a genuinely exercised production flood-wait window from purely synthetic
# tests. An observation is a rejection, never proof an upload was accepted.
_mtproto_flood_observations = Table(
    "storage_mtproto_flood_observations", _meta,
    Column("account", String(255), primary_key=True),
    Column("chat_id", BigInteger, primary_key=True),
    Column("observations", Integer, nullable=False),
    Column("last_observed_ms", BigInteger, nullable=False),
    Column("last_retry_after_seconds", Integer, nullable=False),
)


class MtprotoRateGuard:
    """Durable account/channel rate fencing for the Telethon USER transport."""

    def __init__(self, database: Any):
        self.db = database
        _meta.create_all(database.engine)

    def reserve_send_slot(self, account: str, chat_id: int,
                          interval_seconds: float, *,
                          now_ms: int | None = None) -> float:
        """Reserve one globally serialized per-chat send start time.

        Returns a nonnegative delay in seconds. Shared SQL serialization
        prevents independent workers from all starting Telegram sends in
        the same second; only an actual FloodWaitError means a definite
        rejection. Hosts must have synchronized wall clocks. No slot grants
        exclusive ownership of a message or authorizes retransmission.
        """
        if (not account or type(chat_id) is not int or not chat_id
                or not 0 <= interval_seconds <= 60):
            raise ValueError("Invalid MTProto channel rate interval")
        gap = int(interval_seconds * 1000 + 0.5)
        # Even when local pacing is disabled, allocate a SQL slot row:
        # a definitive flood-wait must remain visible to every worker.
        timestamp = int(time.time() * 1000) if now_ms is None else now_ms
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("Invalid shared MTProto time")
        predicate = (
            _mtproto_chat_slots.c.account == account,
            _mtproto_chat_slots.c.chat_id == chat_id,
        )
        # Begin with a WRITE, not a read: concurrent SQLite workers must
        # not upgrade a stale WAL snapshot to a writer. On PostgreSQL and
        # MySQL the UPDATE takes the row lock for the entire reservation.
        for attempt in range(12):
            try:
                row = self.db.connection.execute(update(_mtproto_chat_slots).where(
                    *predicate
                ).values(next_allowed_ms=case(
                    (_mtproto_chat_slots.c.next_allowed_ms > timestamp,
                     _mtproto_chat_slots.c.next_allowed_ms + gap),
                    else_=timestamp + gap,
                )))
                if row.rowcount == 1:
                    next_ms = self.db.connection.execute(select(
                        _mtproto_chat_slots.c.next_allowed_ms
                    ).where(*predicate)).scalar_one()
                    self.db.commit()
                    return max(0.0, (int(next_ms) - gap - timestamp) / 1000)
                # First reservation for this pair. If another worker won
                # the unique insert, rollback and retry the UPDATE.
                self.db.connection.execute(insert(_mtproto_chat_slots).values(
                    account=account, chat_id=chat_id,
                    next_allowed_ms=timestamp + gap,
                ))
                self.db.commit()
                return 0.0
            except (SQLIntegrityError, SQLOperationalError):
                self.db.connection.rollback()
                if attempt == 11:
                    raise RuntimeError(
                        "Shared MTProto rate reservation failed after bounded retries"
                    ) from None
                time.sleep(0.015 * (attempt + 1))
        raise RuntimeError("Shared MTProto rate reservation could not finish")

    def impose_shared_cooldown(self, account: str, chat_id: int,
                               retry_after: int, *,
                               now_ms: int | None = None) -> None:
        """Atomically publish a definitive flood-wait across all workers.

        A worker may already hold a prebooked send slot. Both the next
        reservation timestamp and the independent wait-before-send fence
        therefore commit in ONE transaction; neither may become visible
        separately after a crash or SQL exception.
        """
        if (type(retry_after) is not int or not 1 <= retry_after <= 3600
                or not account or type(chat_id) is not int or not chat_id):
            raise ValueError("Invalid MTProto flood-wait cooldown")
        timestamp = int(time.time() * 1000) if now_ms is None else now_ms
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("Invalid MTProto flood-wait timestamp")
        until = timestamp + retry_after * 1000
        predicate = (
            _mtproto_chat_cooldowns.c.account == account,
            _mtproto_chat_cooldowns.c.chat_id == chat_id,
        )
        for attempt in range(12):
            try:
                row = self.db.connection.execute(update(_mtproto_chat_slots).where(
                    _mtproto_chat_slots.c.account == account,
                    _mtproto_chat_slots.c.chat_id == chat_id,
                ).values(next_allowed_ms=case(
                    (_mtproto_chat_slots.c.next_allowed_ms < until, until),
                    else_=_mtproto_chat_slots.c.next_allowed_ms,
                )))
                if row.rowcount != 1:
                    raise RuntimeError("MTProto shared cooldown row missing")
                changed = self.db.connection.execute(update(
                    _mtproto_chat_cooldowns
                ).where(*predicate).values(cooldown_until_ms=case(
                    (_mtproto_chat_cooldowns.c.cooldown_until_ms < until, until),
                    else_=_mtproto_chat_cooldowns.c.cooldown_until_ms,
                )))
                if changed.rowcount != 1:
                    self.db.connection.execute(insert(_mtproto_chat_cooldowns).values(
                        account=account, chat_id=chat_id,
                        cooldown_until_ms=until,
                    ))
                # The counter is an observation of a verified flood-wait,
                # not proof that an upload was accepted. Serialize it with
                # the same SQL writer transaction as the rate-limit fence.
                observed = self.db.connection.execute(
                    update(_mtproto_flood_observations).where(
                        _mtproto_flood_observations.c.account == account,
                        _mtproto_flood_observations.c.chat_id == chat_id,
                    ).values(
                        observations=_mtproto_flood_observations.c.observations + 1,
                        last_observed_ms=case(
                            (_mtproto_flood_observations.c.last_observed_ms < timestamp,
                             timestamp),
                            else_=_mtproto_flood_observations.c.last_observed_ms,
                        ),
                        last_retry_after_seconds=case(
                            (_mtproto_flood_observations.c.last_observed_ms <= timestamp,
                             retry_after),
                            else_=_mtproto_flood_observations.c.last_retry_after_seconds,
                        ),
                    )
                )
                if observed.rowcount != 1:
                    self.db.connection.execute(insert(_mtproto_flood_observations).values(
                        account=account, chat_id=chat_id,
                        observations=1, last_observed_ms=timestamp,
                        last_retry_after_seconds=retry_after,
                    ))
                self.db.commit()
                return
            except (SQLIntegrityError, SQLOperationalError):
                self.db.connection.rollback()
                if attempt == 11:
                    raise RuntimeError(
                        "Shared MTProto cooldown failed after bounded retries"
                    ) from None
                time.sleep(0.015 * (attempt + 1))
            except BaseException:
                self.db.connection.rollback()
                raise

    def total_explicit_flood_observations(self) -> int:
        """Non-sensitive SQL-wide count of definite flood-wait rejections.

        Compare readings at the beginning and end of a real-host soak.
        Synthetic tests must never be presented as production flood evidence.
        """
        value = self.db.connection.execute(
            select(func.coalesce(
                func.sum(_mtproto_flood_observations.c.observations), 0,
            ))
        ).scalar_one()
        self.db.commit()
        return int(value)

    def shared_cooldown_wait(self, account: str, chat_id: int, *,
                             now_ms: int | None = None) -> float:
        """Observe a newer flood-wait even after reserving an older send start.

        This is read-only (no remote I/O) and releases the SQL read
        transaction promptly so the next check sees concurrent changes.
        """
        if not account or type(chat_id) is not int or not chat_id:
            raise ValueError("Invalid MTProto shared cooldown identity")
        timestamp = int(time.time() * 1000) if now_ms is None else now_ms
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("Invalid MTProto shared cooldown timestamp")
        deadline = self.db.connection.execute(select(
            _mtproto_chat_cooldowns.c.cooldown_until_ms,
        ).where(
            _mtproto_chat_cooldowns.c.account == account,
            _mtproto_chat_cooldowns.c.chat_id == chat_id,
        )).scalar_one_or_none()
        self.db.commit()
        return max(0.0, (int(deadline or 0) - timestamp) / 1000)
