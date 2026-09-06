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
from urllib.parse import parse_qs, urlparse

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
    title_score: float = 0.0


ProgressCallback = Callable[[float | None, str], None]


INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACE_RUN = re.compile(r"\s+")
# A single Latin letter/digit stuck to the end of a non-ASCII title, e.g. the
# "A" in "魔性の女A". YouTube search sometimes returns nothing for such a token
# next to certain words, so a relaxed query drops it.
TRAILING_LATIN = re.compile(r"(?<=[^\x00-\x7f])[A-Za-z0-9](?=\s*$)")


def canonical_youtube_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    video_id = ""
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("YouTube動画のURLを入力してください。")
    if host == "youtu.be":
        video_id = parsed.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            video_id = parsed.path.split("/")[2]
    if not re.fullmatch(r"[\w-]{11}", video_id, re.ASCII):
        raise ValueError("有効なYouTube動画のURLを入力してください。")
    return f"https://www.youtube.com/watch?v={video_id}"


# Decorative tags stripped before the title is compared. Lyric / cover / live
# markers are deliberately NOT listed here: _noise_penalty needs to still see
# them so those uploads get pushed down the ranking.
BRACKET_NOISE = re.compile(
    r"[\[(](official\s*(music\s*)?video|official\s*audio|audio only|audio|"
    r"mv|m/?v|hd|hq|4k|full\s*ver(sion)?|完全版|フル)[\])]",
    re.IGNORECASE,
)

# Terms that mark a candidate as the wrong kind of upload. They are matched
# against the normalised title and the uploader name. ASCII single words are
# matched as whole tokens; phrases and non-ASCII terms as substrings.
LYRIC_TERMS = ("lyric", "lyrics", "歌詞", "字幕", "가사", "cc字幕")
COVER_TERMS = ("cover", "covered", "カバー", "歌ってみた", "唄ってみた", "弾いてみた",
               "叩いてみた", "cover by", "歌わせて", "を歌う", "うたってみた")
KARAOKE_TERMS = ("karaoke", "カラオケ", "instrumental", "インスト", "inst", "off vocal",
                 "offvocal", "オフボーカル", "backing track", "music box", "オルゴール")
EDIT_TERMS = ("remix", "リミックス", "nightcore", "sped up", "spedup", "slowed", "8d audio",
              "bass boosted", "mashup", "マッシュアップ", "作業用", "1時間", "1 hour",
              "10 hours", "loop", "ループ", "耐久", "つなぎ")
LIVE_TERMS = ("live", "ライブ", "ライヴ", "concert", "コンサート", "公演", "ツアー",
              "弾き語り", "セッション")

# Small set of self-explanatory official distributor channels. Kept short on
# purpose: the load-bearing signals are "uploader == artist", "<artist> - Topic"
# and "<artist>VEVO", all of which verify against the track's own metadata.
LABEL_HINTS = ("universal music", "sony music", "avex", "warner music", "pony canyon")


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
    """Overall ranking score for a YouTube search result."""
    return assess_candidate(track, candidate)[0]


def assess_candidate(track: Track, candidate: dict[str, Any]) -> tuple[float, float]:
    """Return ``(overall score, title-only match 0..1)`` for a search result.

    The song title (with the artist stripped out) dominates the score so that a
    different song by the right artist can never look like a confident match.
    The uploader being the artist, a ``- Topic`` art-track channel or a ``VEVO``
    channel is weighted heavily; lyric videos, covers, karaoke, edits and live
    clips are pushed down.
    """
    title = _normalize(str(candidate.get("title") or ""))
    uploader = _normalize(str(candidate.get("uploader") or candidate.get("channel") or ""))
    artists = [normal for artist in track.artists if (normal := _normalize(artist))]
    want_title = _normalize(track.name)

    stripped = _without(title, artists)
    title_score = max(
        SequenceMatcher(None, want_title, stripped).ratio(),
        SequenceMatcher(None, want_title, title).ratio() * 0.9,
        _coverage(want_title, stripped) * 0.9,
    )

    authority = _uploader_authority(artists, title, uploader)

    duration = float(candidate.get("duration") or 0)
    expected = track.duration_ms / 1000
    if duration and expected:
        difference = abs(duration - expected)
        duration_score = max(0.0, 1.0 - difference / max(expected, 1)) * 0.24
        if difference > 30:
            duration_score -= min(0.4, difference / max(expected, 1) * 0.4)
    else:
        duration_score = -0.05

    penalty = _noise_penalty(candidate, title, uploader)
    overall = title_score * 1.15 + authority + duration_score - penalty
    return overall, title_score


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = BRACKET_NOISE.sub(" ", value)
    return SPACE_RUN.sub(" ", re.sub(r"[^\w]+", " ", value)).strip()


def _without(text: str, phrases: list[str]) -> str:
    for phrase in phrases:
        if phrase and phrase in text:
            text = text.replace(phrase, " ")
    return SPACE_RUN.sub(" ", text).strip()


def _coverage(want: str, have: str) -> float:
    want_tokens = [token for token in want.split() if token]
    if not want_tokens:
        return 0.0
    have_tokens = set(have.split())
    return sum(token in have_tokens for token in want_tokens) / len(want_tokens)


def _compact(value: str) -> str:
    """Drop spaces so CJK names written as ``紫 今`` match ``紫今``."""
    return value.replace(" ", "")


def _matches_term(term: str, text: str, tokens: set[str]) -> bool:
    if " " in term or not term.isascii():
        return term in text
    return term in tokens


def _uploader_authority(artists: list[str], title: str, uploader: str) -> float:
    channel = _compact(uploader)
    topic = channel[:-5] if channel.endswith("topic") else channel
    vevo = channel[:-4] if channel.endswith("vevo") else channel
    compact_title = _compact(title)
    for artist in artists:
        name = _compact(artist)
        if not name:
            continue
        if (name in channel
                or SequenceMatcher(None, name, topic).ratio() > 0.82
                or SequenceMatcher(None, name, vevo).ratio() > 0.82):
            return 0.45
    if any(hint.replace(" ", "") in channel for hint in LABEL_HINTS):
        return 0.28
    if any((name := _compact(artist)) and name in compact_title for artist in artists):
        return 0.1
    return 0.0


def _noise_penalty(candidate: dict[str, Any], title: str, uploader: str) -> float:
    text = f"{title} {uploader}"
    tokens = set(text.split())
    penalty = 0.0
    if any(_matches_term(term, text, tokens) for term in LYRIC_TERMS):
        penalty += 0.15
    if any(_matches_term(term, text, tokens) for term in COVER_TERMS):
        penalty += 0.55
    if any(_matches_term(term, text, tokens) for term in KARAOKE_TERMS):
        penalty += 0.55
    if any(_matches_term(term, text, tokens) for term in EDIT_TERMS):
        penalty += 0.5
    if any(_matches_term(term, title, set(title.split())) for term in LIVE_TERMS):
        penalty += 0.3
    if candidate.get("is_live") or candidate.get("live_status") == "is_live":
        penalty += 0.6
    return penalty


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
        fallback = f"track-{track.position:02d}"
        filename = safe_filename(track.name, fallback, 170)
        repeats = 0
        for other in playlist.tracks:
            if other is track:
                break
            if safe_filename(other.name, f"track-{other.position:02d}", 170) == filename:
                repeats += 1
        if repeats:
            filename = safe_filename(f"{track.name} ({repeats + 1})", fallback, 170)
        return folder / f"{filename}.mp3"

    def download_track(self, playlist: Playlist, track: Track) -> tuple[Path, bool]:
        self._check_cancelled()
        target = self.target_path(playlist, track)
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.config.skip_existing and not track.replace_existing and target.exists() and target.stat().st_size > 0:
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
            self._check_cancelled()
            produced.replace(target)
            track.replace_existing = False
        try:
            temp_parent.rmdir()
        except OSError:
            pass
        return target, False

    def search(self, track: Track) -> SearchCandidate:
        ranked = self._ranked(track, "", max(1, min(self.config.search_results, 10)))
        if not ranked:
            raise DownloadError("YouTubeで候補が見つかりませんでした。")
        return ranked[0]

    def search_candidates(self, track: Track, query: str = "", limit: int = 6) -> list[SearchCandidate]:
        return self._ranked(track, query.strip(), limit)

    def _query_variants(self, track: Track, user_query: str) -> list[str]:
        artist, title = track.artist_text.strip(), track.name.strip()
        loose_title = TRAILING_LATIN.sub("", title).strip()
        raw = [
            user_query,
            f"{artist} - {title} official audio" if not user_query else "",
            f"{artist} {title}",
            f"{title} {artist}",
            f"{artist} {loose_title}" if loose_title and loose_title != title else "",
            title,
        ]
        variants: list[str] = []
        for candidate in raw:
            collapsed = " ".join(candidate.split())
            if collapsed and collapsed not in variants:
                variants.append(collapsed)
        return variants

    def _ranked(self, track: Track, user_query: str, limit: int) -> list[SearchCandidate]:
        wanted = max(1, min(max(limit, self.config.search_results), 15))
        entries: list[dict[str, Any]] = []
        for query in self._query_variants(track, user_query):
            entries = self._search_entries(f"ytsearch{wanted}:{query}")
            if entries:
                break
        candidates: list[SearchCandidate] = []
        for entry in entries:
            video_id = str(entry.get("id") or "")
            url = str(entry.get("webpage_url") or entry.get("url") or "")
            if url and not url.startswith("http") and video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
            elif not url and video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
            if not url:
                continue
            overall, title_score = assess_candidate(track, entry)
            candidates.append(SearchCandidate(
                url=url,
                title=str(entry.get("title") or ""),
                uploader=str(entry.get("uploader") or entry.get("channel") or ""),
                duration=float(entry.get("duration") or 0),
                score=overall,
                thumbnail_url=_thumbnail_url(entry),
                title_score=title_score,
            ))
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return candidates[:limit]

    def _search_entries(self, target: str) -> list[dict[str, Any]]:
        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
            "noplaylist": True,
        }
        last: Exception | None = None
        for attempt in range(3):
            self._check_cancelled()
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(target, download=False)
                return [entry for entry in (info or {}).get("entries") or [] if entry]
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - transient network / extractor failures
                last = exc
                if self.cancel_event.is_set():
                    raise DownloadCancelled("キャンセルしました。") from exc
                if attempt < 2 and self.cancel_event.wait(1.5 * (attempt + 1) + 0.5):
                    raise DownloadCancelled("キャンセルしました。")
        raise DownloadError(f"YouTube検索に失敗しました: {last}")

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

    def candidate_from_url(self, track: Track, value: str) -> SearchCandidate:
        url = canonical_youtube_url(value)
        self._check_cancelled()
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                                   "noplaylist": True, "socket_timeout": 20}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise DownloadError(f"動画情報を取得できません: {exc}") from exc
        self._check_cancelled()
        if not info or info.get("is_live"):
            raise DownloadError("公開済みの通常の動画を指定してください。")
        overall, title_score = assess_candidate(track, info)
        return SearchCandidate(url, str(info.get("title") or ""), str(info.get("uploader") or info.get("channel") or ""),
                               float(info.get("duration") or 0), overall, _thumbnail_url(info), title_score)

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
