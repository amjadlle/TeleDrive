#!/usr/bin/env python3
"""Secure, one-time bootstrap of TeleDrive's real Telegram E2E test secrets.

Prerequisites:
  python -m pip install "Telethon>=1.36,<2"
  gh auth login
  python scripts/setup_telegram_e2e.py --channel "TeleDrive E2E Storage"

Telethon asks for a login code on first sign-in, and for 2FA only if enabled.
The StringSession is never printed or written to disk.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import shutil
import subprocess
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import CreateChannelRequest


def require_github_cli(repo: str) -> None:
    if not shutil.which("gh"):
        raise RuntimeError(
            "GitHub CLI (gh) is not installed. Install it from https://cli.github.com/"
        )
    checks = (
        (["gh", "auth", "status"], "Sign in with 'gh auth login' first."),
        (["gh", "repo", "view", repo, "--json", "nameWithOwner"],
         f"GitHub repository {repo!r} is unavailable to this GitHub login."),
    )
    for command, message in checks:
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(message)


def store_secret(repo: str, name: str, value: str) -> None:
    """Pass the secret on stdin, never via command-line arguments or files."""
    subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo],
        input=value,
        text=True,
        check=True,
        stdout=subprocess.DEVNULL,
    )


async def find_channel(client: TelegramClient, title: str) -> list:
    matches = []
    async for dialog in client.iter_dialogs():
        if (dialog.is_channel and not dialog.is_group
                and dialog.name.casefold() == title.casefold()):
            matches.append(dialog)
    return matches


async def setup(repo: str, title: str, create_if_missing: bool) -> None:
    require_github_cli(repo)

    api_id = int(input("Telegram test account API ID: ").strip())
    api_hash = getpass.getpass("Telegram test account API hash: ").strip()
    phone = input("Telegram TEST account phone (+country code): ").strip()
    if not api_hash or not phone:
        raise ValueError("API hash and test account phone must not be empty.")

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start(
            phone=phone,
            code_callback=lambda: input("Telegram login code: ").strip(),
            password=lambda: getpass.getpass("Telegram 2FA password (only if enabled): "),
        )

        user = await client.get_me()
        if not user or user.bot:
            raise RuntimeError("A dedicated Telegram USER account is required.")

        matches = await find_channel(client, title)
        if not matches and create_if_missing:
            print(f"Creating a private broadcast test channel named {title!r}...")
            await client(CreateChannelRequest(
                title=title,
                about="Private TeleDrive automated storage integration tests",
                broadcast=True,
                megagroup=False,
            ))
            matches = await find_channel(client, title)

        if len(matches) != 1:
            if not matches:
                raise RuntimeError(
                    f"No private channel named {title!r} is visible to this test account. "
                    "Create the channel first, or rerun with --create-if-missing."
                )
            raise RuntimeError(
                f"{len(matches)} channels are named {title!r}; use a unique channel title."
            )

        if not matches[0].entity.broadcast:
            raise RuntimeError("The target is not a broadcast channel.")

        session = client.session.save()
        if not session:
            raise RuntimeError("No authorized StringSession was created.")

        values = {
            "TELEDRIVE_E2E_API_ID": str(api_id),
            "TELEDRIVE_E2E_API_HASH": api_hash,
            "TELEDRIVE_E2E_SESSION": session,
            "TELEDRIVE_E2E_CHANNEL_NAME": title,
        }
        for secret_name, secret_value in values.items():
            store_secret(repo, secret_name, secret_value)
            print(f"Saved GitHub Actions secret: {secret_name}")

        print(
            "\nE2E credentials configured. No session or API hash was displayed.\n"
            "Run the manual Telegram Storage E2E workflow after its YAML file "
            "is available on the repository default branch."
        )
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="babico/TeleDrive")
    parser.add_argument("--channel", default="TeleDrive E2E Storage",
                        help="Unique private Telegram broadcast channel title")
    parser.add_argument("--create-if-missing", action="store_true",
                        help="Create the private broadcast channel on the test account")
    args = parser.parse_args()
    if not args.channel.strip():
        parser.error("--channel cannot be empty")
    try:
        asyncio.run(setup(args.repo, args.channel.strip(), args.create_if_missing))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
