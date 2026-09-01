# TeleDrive Knowledge Base

## Overview
TeleDrive is a safe, resumable, and autonomous personal file uploader and backup client for Windows and Linux. It turns Telegram into a private, unlimited cloud storage drive by uploading files directly to your personal Saved Messages or any private Telegram Channel.

## Key Features
- **Zero Third-Party Cloud Storage Costs**: Uses your own Telegram account storage via the official MTProto API.
- **Flood-Wait Shield**: Intelligent rate-limiting protection. When Telegram returns a FLOOD_WAIT penalty, TeleDrive automatically pauses and waits for the exact required cooldown plus safety jitter before resuming.
- **Human-like Jitter Delays**: Configurable randomized delays (e.g., 20s - 60s) between file uploads to keep account activity natural and safe.
- **ACID-Compliant SQLite Queue**: Scanned files, hashes, sizes, and upload progress are persisted locally in SQLite (`uploader.db`). If your computer restarts, updates, or loses power, TeleDrive resumes right where it left off without duplicate uploads.
- **Standalone Desktop App**: Built with PySide6 (Qt). Windows users can install via `.exe` installer with zero Python dependencies needed. Linux x86_64 tarball package is also available.
- **Open Source**: MIT Licensed repository on GitHub at `https://github.com/amjadlle/TeleDrive`.

## Installation & Setup
### Windows
1. Download `TeleDrive-Setup-1.0.6.exe` from GitHub releases or run:
   ```powershell
   irm "https://github.com/amjadlle/TeleDrive/releases/download/v1.0.6/TeleDrive-Setup-1.0.6.exe" -OutFile "$env:TEMP\TeleDrive-Setup.exe"; Start-Process "$env:TEMP\TeleDrive-Setup.exe"
   ```
2. Launch TeleDrive and enter your Telegram `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org).
3. Authenticate with your phone number and SMS/Telegram OTP code (2FA password supported).
4. Select a local folder to scan and click **Run Batch** or **Start Loop**.

### Linux
Download the `TeleDrive-linux-x86_64.tar.gz` package, extract, and execute `TeleDrive`.

## Frequently Asked Questions (FAQ)
- **Do I need a Telegram Bot / BotFather token?** No. TeleDrive logs in as your regular user account via MTProto API to access unlimited cloud storage.
- **Can I upload to a channel?** Yes. Enter `@channel_username` or `-100xxxxxxxxxx` ID as the target.
- **Is 2-Step Verification (2FA) supported?** Yes. TeleDrive securely handles cloud passwords during login.
- **Where are my credentials saved?** Stored 100% locally on your machine in the app configuration folder.

## Creator Info
- **Creator**: Amjad P A (@amjadlle)
- **Portfolio**: [amjad.mapki.in](https://amjad.mapki.in)
- **GitHub**: [github.com/amjadlle/TeleDrive](https://github.com/amjadlle/TeleDrive)
- **Email**: hire.amjad@gmail.com
