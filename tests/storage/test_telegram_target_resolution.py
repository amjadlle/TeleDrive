"""Offline checks for Telegram E2E destination selection."""

import asyncio
from types import SimpleNamespace

import pytest

from teledrive.storage.target_resolver import resolve_telegram_target


class FakeTelegramClient:
    def __init__(self, dialogs=()):
        self.dialogs = list(dialogs)
        self.entity_requests = []

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog

    async def get_entity(self, target):
        self.entity_requests.append(target)
        return ("resolved", target)


def channel(title="TeleDrive E2E Storage", *, public=False, is_group=False, is_channel=True):
    entity = SimpleNamespace(username="public_username" if public else None)
    return SimpleNamespace(
        id=-1001234567890,
        name=title,
        entity=entity,
        is_channel=is_channel,
        is_group=is_group,
    )


def test_private_channel_name_resolves_from_dialogs():
    expected = channel(title="TeleDrive E2E Storage")
    client = FakeTelegramClient([expected])
    result = asyncio.run(resolve_telegram_target(
        client, "-1009876543210", "  teledrive e2e storage  "
    ))
    assert result is expected.entity
    assert client.entity_requests == []


def test_private_channel_name_rejects_no_match():
    client = FakeTelegramClient([channel(title="Unrelated")])
    with pytest.raises(ValueError, match="No broadcast channel"):
        asyncio.run(resolve_telegram_target(client, None, "TeleDrive E2E Storage"))
    assert client.entity_requests == []


def test_private_channel_name_rejects_duplicates():
    client = FakeTelegramClient([channel(), channel()])
    with pytest.raises(ValueError, match="Multiple Telegram broadcast channels"):
        asyncio.run(resolve_telegram_target(client, None, "TeleDrive E2E Storage"))


@pytest.mark.parametrize("wrong_channel", [
    channel(public=True),
    channel(is_group=True),
    channel(is_channel=False),
])
def test_private_channel_name_rejects_public_groups_and_chats(wrong_channel):
    client = FakeTelegramClient([wrong_channel])
    with pytest.raises(ValueError, match="No broadcast channel"):
        asyncio.run(resolve_telegram_target(client, None, "TeleDrive E2E Storage"))


def test_private_channel_id_uses_joined_dialog_entity():
    expected = channel()
    client = FakeTelegramClient([expected])
    result = asyncio.run(resolve_telegram_target(client, "  -1001234567890  "))
    assert result is expected.entity
    assert client.entity_requests == []


def test_unknown_private_channel_fails_closed():
    client = FakeTelegramClient([channel()])
    with pytest.raises(ValueError, match="not in the test account's dialogs"):
        asyncio.run(resolve_telegram_target(client, "-1002222222222"))
    assert client.entity_requests == []


@pytest.mark.parametrize("value, expected", [
    (None, "me"),
    ("   ", "me"),
    (" @teledrive_e2e ", "@teledrive_e2e"),
])
def test_non_numeric_target_uses_telethon_entity_resolver(value, expected):
    client = FakeTelegramClient()
    assert asyncio.run(resolve_telegram_target(client, value)) == ("resolved", expected)
    assert client.entity_requests == [expected]
