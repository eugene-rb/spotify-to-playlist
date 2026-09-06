$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Workspace ".venv\Scripts\pythonw.exe"

if (-not (Test-Path -LiteralPath $Pythonw)) {
    throw "先に scripts\setup.ps1 を実行してください。"
}

Start-Process -FilePath $Pythonw -ArgumentList "-m", "playlist_audio_saver" -WorkingDirectory $Workspace

