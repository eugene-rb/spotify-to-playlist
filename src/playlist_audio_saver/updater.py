from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from packaging.version import InvalidVersion, Version


GITHUB_REPOSITORY = "eugene-rb/spotify-to-playlist"
UPDATE_ASSET_NAME = "PlaylistAudioSaver-win64.zip"
UPDATE_MANIFEST_NAME = "update.json"
EXECUTABLE_NAME = "PlaylistAudioSaver.exe"


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    version: str
    tag: str
    notes: str
    page_url: str
    download_url: str
    digest: str
    size: int


def newer_version(latest: str, current: str) -> bool:
    try:
        return Version(latest.lstrip("v")) > Version(current.lstrip("v"))
    except InvalidVersion:
        return False


class GitHubUpdater:
    def __init__(self, current_version: str, repository: str = GITHUB_REPOSITORY) -> None:
        self.current_version = current_version
        self.repository = repository
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": f"PlaylistAudioSaver/{current_version}",
            }
        )

    def check(self) -> ReleaseInfo | None:
        try:
            response = self.session.get(
                f"https://github.com/{self.repository}/releases/latest/download/{UPDATE_MANIFEST_NAME}",
                timeout=20,
            )
        except requests.RequestException as exc:
            raise UpdateError(f"更新情報へ接続できません: {exc}") from exc
        if response.status_code == 404:
            return None
        if not response.ok:
            raise UpdateError(f"GitHub Releases APIエラー ({response.status_code})")
        try:
            payload = response.json()
        except requests.JSONDecodeError as exc:
            raise UpdateError("更新マニフェストを読み取れません。") from exc
        tag = str(payload.get("tag_name") or "")
        if not tag or not newer_version(tag, self.current_version):
            return None
        if any(character in tag for character in ("/", "\\", "?", "#")):
            raise UpdateError("更新マニフェストのバージョンが不正です。")
        digest = str(payload.get("sha256") or "").lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise UpdateError("更新ファイルにSHA-256ダイジェストがありません。")
        download_url = (
            f"https://github.com/{self.repository}/releases/download/{tag}/{UPDATE_ASSET_NAME}"
        )
        return ReleaseInfo(
            version=tag.lstrip("v"),
            tag=tag,
            notes=str(payload.get("notes") or ""),
            page_url=f"https://github.com/{self.repository}/releases/tag/{tag}",
            download_url=download_url,
            digest=digest,
            size=int(payload.get("size") or 0),
        )

    def download(
        self,
        release: ReleaseInfo,
        progress: Callable[[float], None],
        cancel_event: threading.Event,
    ) -> Path:
        update_root = Path(tempfile.gettempdir()) / f"PlaylistAudioSaver-update-{uuid.uuid4().hex}"
        archive = update_root / UPDATE_ASSET_NAME
        staging = update_root / "staging"
        update_root.mkdir(parents=True)
        digest = hashlib.sha256()
        downloaded = 0
        try:
            with self.session.get(release.download_url, stream=True, timeout=(20, 60)) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length") or release.size or 0)
                with archive.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if cancel_event.is_set():
                            raise UpdateCancelled("更新をキャンセルしました。")
                        if not chunk:
                            continue
                        output.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        progress(downloaded / total if total else 0)
        except UpdateCancelled:
            shutil.rmtree(update_root, ignore_errors=True)
            raise
        except (OSError, requests.RequestException) as exc:
            shutil.rmtree(update_root, ignore_errors=True)
            raise UpdateError(f"更新ファイルを取得できません: {exc}") from exc
        if digest.hexdigest().lower() != release.digest:
            shutil.rmtree(update_root, ignore_errors=True)
            raise UpdateError("更新ファイルのSHA-256が一致しないため、適用を中止しました。")
        staging.mkdir()
        try:
            with zipfile.ZipFile(archive) as bundle:
                staging_root = staging.resolve()
                for member in bundle.infolist():
                    target = (staging / member.filename).resolve()
                    if not target.is_relative_to(staging_root):
                        raise UpdateError("更新ZIPに不正なパスが含まれています。")
                bundle.extractall(staging)
        except UpdateError:
            shutil.rmtree(update_root, ignore_errors=True)
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            shutil.rmtree(update_root, ignore_errors=True)
            raise UpdateError(f"更新ZIPを展開できません: {exc}") from exc
        if not (staging / EXECUTABLE_NAME).is_file():
            shutil.rmtree(update_root, ignore_errors=True)
            raise UpdateError("更新ZIP内に実行ファイルがありません。")
        progress(1.0)
        return staging


def launch_update_and_restart(staging: Path) -> None:
    if not getattr(sys, "frozen", False):
        raise UpdateError("自動更新の適用はパッケージ版アプリでのみ利用できます。")
    executable = Path(sys.executable).resolve()
    target = executable.parent
    staging = staging.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if executable.name.lower() != EXECUTABLE_NAME.lower():
        raise UpdateError("実行ファイル名を確認できないため更新を中止しました。")
    if target == Path(target.anchor) or not staging.is_relative_to(temp_root):
        raise UpdateError("更新先または一時フォルダーが安全ではありません。")
    script = staging.parent / "apply-update.ps1"
    script.write_text(
        """param([int]$AppPid, [string]$Source, [string]$Target, [string]$Executable)
$ErrorActionPreference = 'Stop'
for ($i = 0; $i -lt 240; $i++) {
    if (-not (Get-Process -Id $AppPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
}
Start-Sleep -Milliseconds 500
Copy-Item -Path (Join-Path $Source '*') -Destination $Target -Recurse -Force
Start-Process -FilePath (Join-Path $Target $Executable) -WorkingDirectory $Target
Start-Sleep -Seconds 1
Remove-Item -LiteralPath (Split-Path $Source -Parent) -Recurse -Force -ErrorAction SilentlyContinue
""",
        encoding="utf-8-sig",
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script), "-AppPid", str(os.getpid()), "-Source", str(staging),
                "-Target", str(target), "-Executable", EXECUTABLE_NAME,
            ],
            creationflags=creation_flags,
            close_fds=True,
        )
    except OSError as exc:
        raise UpdateError(f"更新プログラムを開始できません: {exc}") from exc
