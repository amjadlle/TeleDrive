#!/usr/bin/env python3
"""Secure, one-time bootstrap of TeleDrive's real Telegram E2E test secrets.

Prerequisites:
  python -m pip install "Telethon>=1.36,<2"
  gh auth login
  python scripts/setup_telegram_e2e.py --channel "TeleDrive E2E Storage" --create-if-missing --configure-bot

Telethon asks for a login code on first sign-in, and for 2FA only if enabled.
The StringSession and test channel ID are never printed or written to disk.
Use --secure-bot-chat-id to move the existing unmasked Actions variable
into a repository secret without disclosing the value or logging in to Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shutil
import subprocess
import sys

from typing import TYPE_CHECKING

# Secret migration must remain usable on a bare operator Python install.
# Telegram SDK imports are needed only for actual Telegram login/setup.
if TYPE_CHECKING:
    from telethon import TelegramClient


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


def legacy_bot_chat_variable_names(repo: str) -> set[str]:
    """Read only Actions variable names; never expose their stored values."""
    # gh variable list supports names-only JSON, but has no --limit option.
    command = ["gh", "variable", "list", "--repo", repo, "--json", "name"]
    listed = subprocess.run(command, capture_output=True, check=False)
    if listed.returncode:
        raise RuntimeError("Cannot inspect legacy Actions variable names")
    try:
        rows = json.loads(listed.stdout)
        if not isinstance(rows, list) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
            for entry in rows
        ):
            raise ValueError("Malformed variable list")
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Cannot verify legacy Actions variable list") from exc
    return {entry["name"] for entry in rows}


def remove_public_bot_chat_variable(repo: str) -> None:
    """Delete the unmasked legacy variable and re-list to verify absence."""
    name = "TELEDRIVE_E2E_BOT_CHAT_ID"
    if name in legacy_bot_chat_variable_names(repo):
        command = ["gh", "variable", "delete", name, "--repo", repo]
        removed = subprocess.run(command, capture_output=True, check=False)
        if removed.returncode:
            raise RuntimeError(
                "Legacy unmasked Actions variable could not be deleted"
            )
    # A successful CLI exit does not prove absence (a racing write can
    # re-create the variable). Fail closed rather than making that claim.
    if name in legacy_bot_chat_variable_names(repo):
        raise RuntimeError("Legacy unmasked Actions variable is still present")


def secure_existing_bot_chat_id(repo: str) -> None:
    """One-time non-echoing migration from Actions variable to Secret.

    Read the existing variable only into Python's captured subprocess
    stdout, never the operator terminal. If the variable is already gone,
    prompt for the peer ID using getpass rather than a CLI argument.
    """
    import re

    require_github_cli(repo)
    fetched = subprocess.run(
        ["gh", "variable", "get", "TELEDRIVE_E2E_BOT_CHAT_ID", "--repo", repo],
        capture_output=True,
        check=False,
    )
    if fetched.returncode == 0:
        try:
            chat_id = fetched.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid stored private channel ID") from exc
    else:
        # A failed 'get' can mean insufficient permissions or network errors,
        # not just an absent variable. A names-only query must confirm absence.
        if "TELEDRIVE_E2E_BOT_CHAT_ID" in legacy_bot_chat_variable_names(repo):
            raise RuntimeError(
                "Legacy Actions variable exists but cannot be securely read"
            )
        chat_id = getpass.getpass(
            "Telegram test channel ID (hidden, old variable unavailable): "
        ).strip()
    if not re.fullmatch(r"-100[0-9]{5,16}", chat_id):
        raise ValueError("Bot target must be a numeric private channel ID")
    store_secret(repo, "TELEDRIVE_E2E_BOT_CHAT_ID", chat_id)
    remove_public_bot_chat_variable(repo)
    print("Saved GitHub Actions secret: TELEDRIVE_E2E_BOT_CHAT_ID")
    print("Verified that the previous unmasked variable is absent.")


async def find_channel(client: TelegramClient, title: str) -> list:
    matches = []
    async for dialog in client.iter_dialogs():
        if (dialog.is_channel and not dialog.is_group
                and not getattr(dialog.entity, "username", None)
                and dialog.name.casefold() == title.casefold()):
            matches.append(dialog)
    return matches


async def setup(repo: str, title: str, create_if_missing: bool,
                configure_bot: bool = False) -> None:
    require_github_cli(repo)
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.channels import CreateChannelRequest

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

        # Bot API requires a numeric private-channel chat_id; it cannot
        # discover private channels by title. The user session can resolve
        # that ID during this one-time setup, so no manual ID lookup.
        channel_id = int(matches[0].id)
        store_secret(repo, "TELEDRIVE_E2E_BOT_CHAT_ID", str(channel_id))
        print("Saved GitHub Actions secret: TELEDRIVE_E2E_BOT_CHAT_ID")
        remove_public_bot_chat_variable(repo)
        if configure_bot:
            bot_token = getpass.getpass("Token from @BotFather (hidden): ").strip()
            if not bot_token:
                raise ValueError("Bot token cannot be empty.")
            # Validate locally before creating the GitHub secret.
            from teledrive.storage.bot_api_adapter import BotApiClient
            bot_api = BotApiClient(bot_token)
            identity = await bot_api.call("getMe", {})
            if not identity.get("is_bot"):
                raise RuntimeError("BotFather token did not authenticate a bot.")
            store_secret(repo, "TELEDRIVE_E2E_BOT_TOKEN", bot_token)
            print("Saved GitHub Actions secret: TELEDRIVE_E2E_BOT_TOKEN")
            print("Ensure this bot is channel ADMIN with post and delete permissions.")

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
    parser.add_argument("--configure-bot", action="store_true",
                        help="Also configure the BotFather token as a GitHub Secret")
    parser.add_argument("--secure-bot-chat-id", action="store_true",
                        help="Move an existing test channel ID into a masked GitHub Secret")
    args = parser.parse_args()
    if not args.channel.strip():
        parser.error("--channel cannot be empty")
    try:
        if args.secure_bot_chat_id:
            secure_existing_bot_chat_id(args.repo)
        else:
            asyncio.run(setup(
                args.repo, args.channel.strip(), args.create_if_missing,
                args.configure_bot,
            ))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
