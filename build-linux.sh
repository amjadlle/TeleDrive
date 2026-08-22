#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt pyinstaller

rm -rf build dist
.venv/bin/python -m PyInstaller --noconfirm --clean --windowed \
  --name "TeleDrive" --hidden-import uploader desktop.py

mkdir -p dist-installer
tar -czf dist-installer/TeleDrive-linux-x86_64.tar.gz -C dist TeleDrive

echo "Linux package created: dist-installer/TeleDrive-linux-x86_64.tar.gz"
