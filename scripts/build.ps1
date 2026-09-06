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
    $AppVersion = & $Python -c "from playlist_audio_saver import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0 -or -not $AppVersion) {
        throw "Could not determine the application version."
    }

    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --console `
        --name "PlaylistAudioBackend" `
        --paths "src" `
        --hidden-import "keyring.backends.Windows" `
        "backend_launcher.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Application build failed (exit code: $LASTEXITCODE)."
    }

    # Clear only the verified generated app directory, including legacy Tk files.
    $AppOutput = [IO.Path]::GetFullPath((Join-Path $Workspace "dist/PlaylistAudioSaver"))
    $ExpectedOutput = [IO.Path]::GetFullPath($Workspace) + [IO.Path]::DirectorySeparatorChar + "dist" + [IO.Path]::DirectorySeparatorChar + "PlaylistAudioSaver"
    if ($AppOutput -ne $ExpectedOutput) { throw "Unexpected app output directory: $AppOutput" }
    if (Test-Path -LiteralPath $AppOutput) {
        Remove-Item -LiteralPath $AppOutput -Recurse -Force
    }
    & dotnet publish "native/PlaylistAudioSaver" -c Release -r win-x64 --self-contained true `
        -o "dist/PlaylistAudioSaver" "-p:Version=$AppVersion" --nologo
    if ($LASTEXITCODE -ne 0) { throw "Native GUI publish failed." }
    $BackendDestination = Join-Path $Workspace "dist/PlaylistAudioSaver/backend"
    New-Item -ItemType Directory -Force -Path $BackendDestination | Out-Null
    Copy-Item -Path "dist/PlaylistAudioBackend/*" -Destination $BackendDestination -Recurse -Force

    & $Iscc "/DAppVersion=$AppVersion" "installer\PlaylistAudioSaver.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Installer build failed (exit code: $LASTEXITCODE)."
    }

    Write-Host "Installer created: dist\installer\PlaylistAudioSaver-Setup.exe"
}
finally {
    Pop-Location
}
