"""Resolve Telegram E2E destinations from private channel titles or peer IDs.

StringSession does not retain Telegram entity caches across processes. Resolve
private channels using the authorized user's dialogs to preserve access hashes.
"""

from __future__ import annotations

from typing import Any


async def resolve_telegram_target(
    client: Any,
    target_spec: str | None,
    channel_name: str | None = None,
) -> Any:
    """Resolve a Telegram destination without guessing private-channel entities.

    If channel_name is configured, find exactly one broadcast channel with the
    specified title in the authenticated account's dialogs. Channel-name
    matching takes precedence over the legacy TELEDRIVE_E2E_TARGET variable.
    """
    name = (channel_name or "").strip()
    if name:
        matches = []
        async for dialog in client.iter_dialogs():
            if (dialog.is_channel and not dialog.is_group
                    and dialog.name.casefold() == name.casefold()):
                matches.append(dialog.entity)
                if len(matches) > 1:
                    break
        if not matches:
            raise ValueError(
                f"No broadcast channel named {name!r} found in the test account's dialogs. "
                "Create or join that private channel using the test account."
            )
        if len(matches) > 1:
            raise ValueError(
                f"Multiple Telegram broadcast channels named {name!r} found. "
                "Give the test channel a unique name."
            )
        return matches[0]

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
