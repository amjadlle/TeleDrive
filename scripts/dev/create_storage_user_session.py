#!/usr/bin/env python3
"""Create a private Telethon USER StringSession for REST/S3 storage.

Run interactively on an owner-controlled machine. The session grants account
access; do not put it in Git, CI logs or Docker environment variables.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import platform
from pathlib import Path


async def authorize(output: Path) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(os.environ["TELEDRIVE_STORAGE_MTPROTO_API_ID"])
    api_hash = os.environ["TELEDRIVE_STORAGE_MTPROTO_API_HASH"].strip()
    if api_id <= 0 or len(api_hash) != 32:
        raise ValueError("Invalid Telegram application API credentials")
    if not output.parent.is_dir() or output.exists() or output.is_symlink():
        raise FileExistsError("Session parent must exist; refuse overwrite")
    client = TelegramClient(
        StringSession(), api_id, api_hash,
        device_model="TeleDrive Storage Client",
        system_version=platform.system(), app_version="TeleDrive 2.1",
        receive_updates=False, flood_sleep_threshold=0,
    )
    try:
        await client.start()  # Interactive user phone, OTP and optional 2FA.
        identity = await client.get_me()
        if identity is None or getattr(identity, "bot", False):
            raise RuntimeError("Use a real Telegram USER session, not a bot token")
        contents = client.session.save()
        if not contents:
            raise RuntimeError("Telegram returned an empty authorized user session")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(output), flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(contents + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    finally:
        await client.disconnect()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=Path("config/mtproto-user.session"))
    args = p.parse_args()
    os.umask(0o077)
    asyncio.run(authorize(args.output))
    print("Authorized user session saved with mode 0600; keep it private.")


if __name__ == "__main__":
    main()
