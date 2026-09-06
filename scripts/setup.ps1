$ErrorActionPreference = "Stop"
$Workspace = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Workspace ".venv"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python Launcher (py.exe) が見つかりません。Python 3.11以降をインストールしてください。"
}

if (-not (Test-Path -LiteralPath $Venv)) {
    py -3 -m venv $Venv
}

$Python = Join-Path $Venv "Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -e "${Workspace}[dev]"
Write-Host "セットアップが完了しました。 scripts\run.ps1 で起動できます。"
