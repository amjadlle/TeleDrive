#define MyAppName "TeleDrive"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "TeleDrive"
#define MyAppExeName "TeleDrive.exe"
#define MyAppDescription "Turn Telegram into your personal unlimited cloud storage."

[Setup]
AppId={{8F15A45F-47A7-4955-944B-E9A60FEB566A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\TeleDrive
DefaultGroupName={#MyAppName}
OutputDir=..\dist-installer
OutputBaseFilename=TeleDrive-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
SetupIconFile=..\installer\TeleDrive.ico

[Files]
Source: "..\dist\TeleDrive.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
