$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Workspace ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "先に scripts\setup.ps1 を実行してください。"
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
    Write-Host "ビルド完了: dist\PlaylistAudioSaver\PlaylistAudioSaver.exe"
}
finally {
    Pop-Location
}
