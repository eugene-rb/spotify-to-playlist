from __future__ import annotations

import re
import shutil
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import requests
import yt_dlp
from mutagen.id3 import (
    APIC,
    COMM,
    TALB,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TSRC,
    TXXX,
    WOAS,
    ID3,
    ID3NoHeaderError,
)

from .config import AppConfig
from .models import Playlist, Track


class DownloadCancelled(RuntimeError):
    pass


class DownloadError(RuntimeError):
    pass


@dataclass(slots=True)
class SearchCandidate:
    url: str
    title: str
    uploader: str
    duration: float
    score: float
    thumbnail_url: str = ""


ProgressCallback = Callable[[float | None, str], None]


INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACE_RUN = re.compile(r"\s+")
BRACKET_NOISE = re.compile(
    r"[\[(](official\s*(music\s*)?video|official\s*audio|lyrics?|audio|mv|hd|4k)[\])]",
    re.IGNORECASE,
)


def safe_filename(value: str, fallback: str = "untitled", max_length: int = 120) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = INVALID_FILE_CHARS.sub("_", value)
    value = SPACE_RUN.sub(" ", value).strip(" .")
    reserved = {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if not value:
        value = fallback
    if value.upper() in reserved:
        value = f"_{value}"
    return value[:max_length].rstrip(" .") or fallback


def ffmpeg_available(configured_path: str = "") -> bool:
    value = configured_path.strip().strip('"')
    if value:
        path = Path(value)
        if path.is_dir():
            return (path / "ffmpeg.exe").is_file() or (path / "ffmpeg").is_file()
        return path.is_file()
    return shutil.which("ffmpeg") is not None


def score_candidate(track: Track, candidate: dict[str, Any]) -> float:
    title = str(candidate.get("title") or "")
    uploader = str(candidate.get("uploader") or candidate.get("channel") or "")
    normalized_title = _normalize(title)
    desired = _normalize(f"{track.artist_text} {track.name}")
    title_only = _normalize(track.name)
    artist = _normalize(track.artists[0]) if track.artists else ""
    similarity = max(
        SequenceMatcher(None, desired, normalized_title).ratio(),
        SequenceMatcher(None, title_only, normalized_title).ratio() * 0.88,
    )
    artist_bonus = 0.14 if artist and (artist in normalized_title or artist in _normalize(uploader)) else 0
    duration = float(candidate.get("duration") or 0)
    expected = track.duration_ms / 1000
    if duration and expected:
        difference = abs(duration - expected)
        duration_score = max(0.0, 1.0 - difference / max(expected, 1)) * 0.24
        if difference > 30:
            duration_score -= min(0.35, difference / max(expected, 1) * 0.35)
    else:
        duration_score = -0.08
    noise_penalty = 0.0
    lowered = normalized_title
    if any(term in lowered for term in ("live", "cover", "remix", "nightcore", "sped up")):
        noise_penalty = 0.18
    if candidate.get("is_live") or candidate.get("live_status") == "is_live":
        noise_penalty += 0.5
    return similarity + artist_bonus + duration_score - noise_penalty


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = BRACKET_NOISE.sub(" ", value)
    return SPACE_RUN.sub(" ", re.sub(r"[^\w]+", " ", value)).strip()


class AudioDownloader:
    def __init__(
        self,
        config: AppConfig,
        cancel_event: threading.Event,
        progress: ProgressCallback,
    ) -> None:
        self.config = config
        self.cancel_event = cancel_event
        self.progress = progress
        self.http = requests.Session()
        self._cover_cache: dict[str, tuple[bytes, str]] = {}

    def target_path(self, playlist: Playlist, track: Track) -> Path:
        folder = self.config.resolved_output_dir / safe_filename(playlist.name, "Spotify Playlist")
        filename = safe_filename(
            f"{track.position:02d} - {track.artist_text} - {track.name}",
            f"track-{track.position:02d}",
            170,
        )
        return folder / f"{filename}.mp3"

    def download_track(self, playlist: Playlist, track: Track) -> tuple[Path, bool]:
        self._check_cancelled()
        target = self.target_path(playlist, track)
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.config.skip_existing and target.exists() and target.stat().st_size > 0:
            return target, True

        if not track.selected_video_url:
            self.progress(None, "YouTubeを検索中")
            candidate = self.search(track)
            apply_candidate(track, candidate)
        self._check_cancelled()

        temp_parent = target.parent / ".partial"
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="track-", dir=temp_parent) as temp_dir:
            temp_base = Path(temp_dir) / "audio"
            options: dict[str, Any] = {
                "format": "bestaudio/best",
                "outtmpl": str(temp_base) + ".%(ext)s",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "retries": 3,
                "fragment_retries": 3,
                "progress_hooks": [self._progress_hook],
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": self.config.audio_quality,
                    }
                ],
            }
            ffmpeg = self.config.ffmpeg_path.strip()
            if ffmpeg:
                options["ffmpeg_location"] = ffmpeg
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([track.selected_video_url])
            except DownloadCancelled:
                raise
            except Exception as exc:
                if self.cancel_event.is_set():
                    raise DownloadCancelled("キャンセルしました。") from exc
                raise DownloadError(f"音声を取得できませんでした: {exc}") from exc
            produced = Path(f"{temp_base}.mp3")
            if not produced.exists():
                matches = list(Path(temp_dir).glob("*.mp3"))
                if not matches:
                    raise DownloadError("変換後のMP3ファイルが見つかりません。FFmpegを確認してください。")
                produced = matches[0]
            self.progress(None, "メタデータを書き込み中")
            self.write_tags(produced, track)
            target.unlink(missing_ok=True)
            shutil.move(str(produced), target)
        try:
            temp_parent.rmdir()
        except OSError:
            pass
        return target, False

    def search(self, track: Track) -> SearchCandidate:
        query = f"{track.artist_text} - {track.name} official audio"
        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(
                    f"ytsearch{max(1, min(self.config.search_results, 10))}:{query}",
                    download=False,
                )
        except Exception as exc:
            raise DownloadError(f"YouTube検索に失敗しました: {exc}") from exc
        self._check_cancelled()
        entries = [entry for entry in (info or {}).get("entries") or [] if entry]
        if not entries:
            raise DownloadError("YouTubeで候補が見つかりませんでした。")
        ranked = sorted(entries, key=lambda entry: score_candidate(track, entry), reverse=True)
        best = ranked[0]
        video_id = str(best.get("id") or "")
        url = str(best.get("webpage_url") or best.get("url") or "")
        if url and not url.startswith("http") and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"
        elif not url and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"
        if not url:
            raise DownloadError("検索候補のURLを取得できませんでした。")
        return SearchCandidate(
            url=url,
            title=str(best.get("title") or ""),
            uploader=str(best.get("uploader") or best.get("channel") or ""),
            duration=float(best.get("duration") or 0),
            score=score_candidate(track, best),
            thumbnail_url=_thumbnail_url(best),
        )

    def fetch_thumbnail(self, url: str) -> bytes:
        if not url:
            return b""
        try:
            response = self.http.get(url, timeout=15)
            response.raise_for_status()
            if len(response.content) > 5_000_000:
                return b""
            return response.content
        except requests.RequestException:
            return b""

    def write_tags(self, path: Path, track: Track) -> None:
        try:
            tags = ID3(path)
            tags.clear()
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=track.name))
        tags.add(TPE1(encoding=3, text=track.artists))
        tags.add(TALB(encoding=3, text=track.album))
        if track.album_artists:
            tags.add(TPE2(encoding=3, text=track.album_artists))
        if track.release_date:
            tags.add(TDRC(encoding=3, text=track.release_date))
        tags.add(TRCK(encoding=3, text=str(track.track_number)))
        tags.add(TPOS(encoding=3, text=str(track.disc_number)))
        if track.isrc:
            tags.add(TSRC(encoding=3, text=track.isrc))
        tags.add(TXXX(encoding=3, desc="SPOTIFY_TRACK_ID", text=track.spotify_id))
        tags.add(TXXX(encoding=3, desc="YOUTUBE_SOURCE", text=track.selected_video_url))
        if track.spotify_url:
            tags.add(WOAS(url=track.spotify_url))
        tags.add(
            COMM(
                encoding=3,
                lang="jpn",
                desc="Spotify attribution",
                text=f"Metadata provided by Spotify. {track.spotify_url}",
            )
        )
        cover = self._get_cover(track.cover_url) if track.cover_url else None
        if cover:
            data, mime = cover
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        tags.save(path, v2_version=3)

    def _get_cover(self, url: str) -> tuple[bytes, str] | None:
        if url in self._cover_cache:
            return self._cover_cache[url]
        try:
            response = self.http.get(url, timeout=20)
            response.raise_for_status()
            mime = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
            if mime not in {"image/jpeg", "image/png"}:
                mime = "image/jpeg"
            value = (response.content, mime)
            self._cover_cache[url] = value
            return value
        except requests.RequestException:
            return None

    def _progress_hook(self, data: dict[str, Any]) -> None:
        self._check_cancelled()
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            ratio = min(downloaded / total, 1.0) if total else None
            speed = data.get("_speed_str", "").strip()
            self.progress(ratio, f"ダウンロード中 {speed}".rstrip())
        elif data.get("status") == "finished":
            self.progress(1.0, "音声へ変換中")

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise DownloadCancelled("キャンセルしました。")


def apply_candidate(track: Track, candidate: SearchCandidate) -> None:
    track.selected_video_url = candidate.url
    track.selected_video_title = candidate.title
    track.selected_video_uploader = candidate.uploader
    track.selected_video_duration = candidate.duration
    track.youtube_thumbnail_url = candidate.thumbnail_url
    track.match_score = candidate.score
    track.excluded = False


def _thumbnail_url(entry: dict[str, Any]) -> str:
    direct = str(entry.get("thumbnail") or "")
    if direct:
        return direct
    thumbnails = entry.get("thumbnails") or []
    if thumbnails:
        return str(thumbnails[-1].get("url") or "")
    video_id = str(entry.get("id") or "")
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" if video_id else ""
