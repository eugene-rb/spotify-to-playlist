param(
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $PSScriptRoot
$Python = if ($PythonPath) { $PythonPath } else { Join-Path $Workspace ".venv\Scripts\python.exe" }

if (-not $PythonPath -and -not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found. Run scripts\setup.ps1 first."
}

$IsccCandidates = @(
    (Get-Command "ISCC.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
$Iscc = $IsccCandidates | Select-Object -First 1

if (-not $Iscc) {
    throw "Inno Setup 6 not found. Install it with: winget install --id JRSoftware.InnoSetup -e --scope user"
}

Push-Location $Workspace
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --name "PlaylistAudioSaver" `
        --paths "src" `
        --hidden-import "keyring.backends.Windows" `
        "launcher.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Application build failed (exit code: $LASTEXITCODE)."
    }

    $AppVersion = & $Python -c "from playlist_audio_saver import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0 -or -not $AppVersion) {
        throw "Could not determine the application version."
    }

    & $Iscc "/DAppVersion=$AppVersion" "installer\PlaylistAudioSaver.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Installer build failed (exit code: $LASTEXITCODE)."
    }

    Write-Host "Installer created: dist\installer\PlaylistAudioSaver-Setup.exe"
}
finally {
    Pop-Location
}
