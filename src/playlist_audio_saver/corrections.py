"""Remembers the YouTube videos the user picked by hand in "候補を訂正".

The store is a plain JSON file next to ``config.json``. It is used three ways:
a saved pick is re-applied on the next match, pinned to the top of the
correction dialog, and the channels the user favoured for an artist add a
small ranking bonus for that artist's other tracks.
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import config_directory
from .models import Track

CORRECTIONS_FILE = "corrections.json"
MAX_ENTRIES = 2000


def _norm(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _track_key(title: str, artists: list[str]) -> str:
    return _norm(title) + "␟" + _norm(", ".join(artists))


@dataclass(slots=True)
class Correction:
    spotify_id: str
    title: str
    artists: list[str]
    duration_ms: int
    video_url: str
    video_title: str
    video_uploader: str
    saved_at: str


class CorrectionStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.path = (directory or config_directory()) / CORRECTIONS_FILE
        self._records: list[Correction] = []
        self._by_id: dict[str, Correction] = {}
        self._by_key: dict[str, Correction] = {}
        self._loaded = False

    # -- reading -----------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("entries", []) if isinstance(raw, dict) else []
        except (OSError, ValueError, TypeError):
            entries = []
        for item in entries if isinstance(entries, list) else []:
            record = _parse(item)
            if record is not None:
                self._records.append(record)
                self._index(record)

    def _index(self, record: Correction) -> None:
        if record.spotify_id:
            self._by_id[record.spotify_id] = record
        self._by_key[_track_key(record.title, record.artists)] = record

    def lookup(self, track: Track) -> Correction | None:
        self._ensure_loaded()
        if track.spotify_id and track.spotify_id in self._by_id:
            return self._by_id[track.spotify_id]
        return self._by_key.get(_track_key(track.name, track.artists))

    def preferred_uploaders(self, track: Track) -> frozenset[str]:
        self._ensure_loaded()
        wanted = {_norm(artist) for artist in track.artists if artist.strip()}
        if not wanted:
            return frozenset()
        names = {
            _norm(record.video_uploader)
            for record in self._records
            if record.video_uploader and wanted & {_norm(a) for a in record.artists}
        }
        return frozenset(name for name in names if name)

    @property
    def count(self) -> int:
        self._ensure_loaded()
        return len(self._records)

    # -- writing ---------------------------------------------------------
    def remember(self, track: Track, *, video_url: str, video_title: str, video_uploader: str) -> None:
        self._ensure_loaded()
        if not video_url:
            return
        record = Correction(
            spotify_id=track.spotify_id,
            title=track.name,
            artists=list(track.artists),
            duration_ms=track.duration_ms,
            video_url=video_url,
            video_title=video_title,
            video_uploader=video_uploader,
            saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        key = _track_key(record.title, record.artists)
        self._records = [
            other for other in self._records
            if not ((record.spotify_id and other.spotify_id == record.spotify_id)
                    or _track_key(other.title, other.artists) == key)
        ]
        self._records.append(record)
        self._index(record)
        self._flush()

    def clear(self) -> None:
        self._loaded = True
        self._records.clear()
        self._by_id.clear()
        self._by_key.clear()
        try:
            self.path.unlink()
        except OSError:
            pass

    def _flush(self) -> None:
        payload = {"version": 1, "entries": [asdict(record) for record in self._records[-MAX_ENTRIES:]]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


def _parse(item: object) -> Correction | None:
    if not isinstance(item, dict):
        return None
    raw_artists = item.get("artists", [])
    try:
        record = Correction(
            spotify_id=str(item.get("spotify_id", "")),
            title=str(item.get("title", "")),
            artists=[str(a) for a in raw_artists] if isinstance(raw_artists, list) else [],
            duration_ms=int(item.get("duration_ms", 0) or 0),
            video_url=str(item.get("video_url", "")),
            video_title=str(item.get("video_title", "")),
            video_uploader=str(item.get("video_uploader", "")),
            saved_at=str(item.get("saved_at", "")),
        )
    except (TypeError, ValueError):
        return None
    return record if record.video_url else None
