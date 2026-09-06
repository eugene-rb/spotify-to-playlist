$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $PSScriptRoot
$NativeApp = Join-Path $Workspace "native\PlaylistAudioSaver\bin\Release\net10.0-windows\PlaylistAudioSaver.exe"

if (-not (Test-Path -LiteralPath $NativeApp)) {
    throw "Native GUI not built. Run scripts\setup.ps1 first."
}

Start-Process -FilePath $NativeApp -WorkingDirectory $Workspace -WindowStyle Hidden
