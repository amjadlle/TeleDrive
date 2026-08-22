# Telegram Auto Upload

Safe, slow, resumable Telegram file uploader for Windows and Linux.

TeleDrive turns Telegram into a personal cloud uploader. It scans a local folder,
queues files in SQLite, and uploads them gradually with retry and flood-wait handling.

## Desktop application

The production UI is a PySide6 desktop app. It stores configuration, the Telegram
session, logs, and SQLite state under:

```text
Windows: `%LOCALAPPDATA%\Telegram Auto Upload`  
Linux: `~/.local/share/Telegram Auto Upload`
```

Run from source:

```powershell
python -m pip install -r requirements.txt
python -m app.main
```

## Screenshots

### Dashboard

![TeleDrive dashboard](images/dashboard.png)

### Upload queue

![TeleDrive upload queue](images/upload-queue.png)

### Settings

![TeleDrive settings](images/settings.png)

## Build on Windows

Create the portable Windows application folder:

```powershell
.\build.ps1
```

Output: `dist\TeleDrive.exe`

This is a standalone executable. You can copy only this `.exe` to another Windows
machine; no Python installation is required.

To create the installer, install Inno Setup 6 and run:

```powershell
.\build-installer.ps1
```

The installer is per-user and does not require administrator privileges.

The installer output is `dist-installer\TeleDrive-Setup-1.0.0.exe`.

## Build on Linux

The same source code runs on Linux with Python, PySide6, Telethon, and PyYAML:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.main
```

Create a Linux executable folder with PyInstaller:

```bash
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed \
  --name "Telegram Auto Upload" --hidden-import uploader desktop.py
```

The Linux output is created under `dist/Telegram Auto Upload/`. The Windows `.exe`
and Inno Setup installer are Windows-specific; they are not used on Linux.

The repository also includes `build-linux.sh`, which automates the Linux build and
creates `dist-installer/TeleDrive-linux-x86_64.tar.gz`:

```bash
bash build-linux.sh
```

After extracting the archive, launch the app with:

```bash
./TeleDrive/TeleDrive
```

To package the Linux build for sharing:

```bash
tar -czf TeleDrive-linux-x86_64.tar.gz -C dist "Telegram Auto Upload"
```

Linux users need to build on Linux (or inside WSL); PyInstaller does not cross-compile
a Linux executable from Windows.

## First use

1. Launch the desktop application.
2. Open Settings.
3. Enter the Telegram API ID and API hash from `my.telegram.org`.
4. Set the target to `me` for a safe Saved Messages test.
5. Choose a small source folder.
6. Save settings and scan the folder.
7. Run one upload batch.

The first login requests the Telegram code and, if enabled, the two-step verification
password. The session is stored locally and reused on later runs.

## Backend CLI

The uploader engine remains available for automation and diagnostics:

```powershell
python uploader.py --config path\to\config.yaml --scan-only
python uploader.py --config path\to\config.yaml --run-once
```

## Safety behavior

- Recursive source-folder scanning
- SQLite queue with resumable state
- One file uploaded at a time
- Configurable delays and daily/run limits
- Retry and flood-wait handling
- Changed or missing files are skipped safely
- Single-process lock prevents overlapping runs

Keep the target private while testing, avoid modifying files during an active upload,
and back up the local state database and Telegram session when migrating machines.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

## Contributing

Bug reports, documentation improvements, and code contributions are welcome. Please
read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
