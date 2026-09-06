from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Track:
    position: int
    spotify_id: str
    name: str
    artists: list[str]
    album: str
    album_artists: list[str]
    release_date: str
    track_number: int
    disc_number: int
    duration_ms: int
    explicit: bool
    isrc: str
    spotify_url: str
    cover_url: str
    status: str = "待機中"
    selected_video_url: str = ""
    selected_video_title: str = ""
    selected_video_uploader: str = ""
    selected_video_duration: float = 0
    youtube_thumbnail_url: str = ""
    match_score: float = 0
    excluded: bool = False

    @property
    def artist_text(self) -> str:
        return ", ".join(self.artists)

    @property
    def duration_text(self) -> str:
        seconds = max(0, self.duration_ms // 1000)
        return f"{seconds // 60}:{seconds % 60:02d}"

    @property
    def youtube_duration_text(self) -> str:
        seconds = max(0, int(self.selected_video_duration))
        return f"{seconds // 60}:{seconds % 60:02d}" if seconds else "--:--"


@dataclass(slots=True)
class Playlist:
    spotify_id: str
    name: str
    owner: str
    spotify_url: str
    cover_url: str
    tracks: list[Track] = field(default_factory=list)
