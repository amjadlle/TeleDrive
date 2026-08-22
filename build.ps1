$ErrorActionPreference = 'Stop'
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller
$icon = if (Test-Path 'installer\TeleDrive.ico') { '--icon', 'installer\TeleDrive.ico' } else { @() }
pyinstaller --noconfirm --clean --onefile --windowed --name "TeleDrive" @icon --hidden-import uploader desktop.py
Write-Host "Build completed: dist\TeleDrive.exe"
