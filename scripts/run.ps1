$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Workspace ".venv\Scripts\pythonw.exe"

if (-not (Test-Path -LiteralPath $Pythonw)) {
    throw "Python environment not found. Run scripts\setup.ps1 first."
}

Start-Process -FilePath $Pythonw -ArgumentList "-m", "playlist_audio_saver" -WorkingDirectory $Workspace
