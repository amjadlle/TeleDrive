$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$compiler = Get-Command ISCC -ErrorAction SilentlyContinue
if (-not $compiler) {
    $fallback = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
    if (Test-Path $fallback) { $compiler = Get-Item $fallback }
}
if (-not $compiler) {
    $fallbacks = @(
        (Join-Path ${env:ProgramFiles} 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:LOCALAPPDATA} 'Programs\Antigravity IDE\resources\app\node_modules\innosetup\bin\ISCC.exe')
    )
    $found = $fallbacks | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($found) { $compiler = Get-Item $found }
}
if (-not $compiler) {
    throw "Inno Setup 6 is required. Install it from https://jrsoftware.org/isinfo.php, then run this script again."
}
$appExe = Join-Path $root 'dist\TeleDrive.exe'
if (-not (Test-Path $appExe)) {
    throw "Application executable not found at '$appExe'. Run build.ps1 first."
}
& $compiler.FullName (Join-Path $root 'installer\TelegramAutoUpload.iss')
$installer = Get-ChildItem (Join-Path $root 'dist-installer') -Filter '*.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $installer) { throw 'Inno Setup finished without producing an installer.' }
Write-Host "Installer completed: $($installer.FullName)"
