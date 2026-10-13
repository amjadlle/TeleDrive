"""Resolve Telegram E2E targets, including private channels by numeric peer ID.

A Telethon StringSession does not persist the entity cache between runs, so
numeric targets are resolved from the authorized account's current dialogs.
"""

from __future__ import annotations

from typing import Any


async def resolve_telegram_target(client: Any, target_spec: str | None) -> Any:
    """Return an entity visible to the authenticated Telegram account.

    Accepts 'me', usernames/invite links, or a numeric peer ID (e.g. -100...).
    Numeric peers must be in the account's dialogs; no guessing access hashes.
    """
    target = (target_spec or "me").strip() or "me"
    if target.lstrip("-").isdigit():
        target_id = int(target)
        async for dialog in client.iter_dialogs():
            if dialog.id == target_id:
                return dialog.entity
        raise ValueError(
            f"Telegram E2E target {target_id} is not in the test account's dialogs. "
            "Join/create the test channel with the same authorized account."
        )
    return await client.get_entity(target)
