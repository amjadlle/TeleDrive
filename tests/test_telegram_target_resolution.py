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


def test_private_channel_id_uses_joined_dialog_entity():
    channel = object()
    client = FakeTelegramClient([SimpleNamespace(id=-1001234567890, entity=channel)])
    result = asyncio.run(resolve_telegram_target(client, "  -1001234567890  "))
    assert result is channel
    assert client.entity_requests == []


def test_unknown_private_channel_fails_closed():
    client = FakeTelegramClient([SimpleNamespace(id=-1001111111111, entity=object())])
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
