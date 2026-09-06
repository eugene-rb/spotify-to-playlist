import json
import threading
from dataclasses import asdict

import pytest

from playlist_audio_saver import backend as backend_module
from playlist_audio_saver.backend import Backend, validated_config
from playlist_audio_saver.config import AppConfig, ConfigStore
from playlist_audio_saver.downloader import AudioDownloader, DownloadCancelled, SearchCandidate, canonical_youtube_url
from playlist_audio_saver.models import Playlist
from playlist_audio_saver.spotify import SpotifyClient, SpotifyError, parse_public_collection, spotify_resource
from test_core import sample_track


@pytest.mark.parametrize("value,expected", [
    ("https://open.spotify.com/playlist/abc?si=123", ("playlist", "abc")),
    ("https://open.spotify.com/intl-ja/album/abc", ("album", "abc")),
    ("https://open.spotify.com/embed/track/abc", ("track", "abc")),
    ("spotify:album:abc", ("album", "abc")),
    ("spotify:track:abc", ("track", "abc")),
])
def test_collection_links(value, expected):
    assert spotify_resource(value) == expected


@pytest.mark.parametrize("value", ["https://example.com/album/abc", "https://open.spotify.com/artist/abc", "spotify:album:../abc", "file:///album/abc"])
def test_invalid_collection_links(value):
    with pytest.raises(ValueError):
        spotify_resource(value)


def embed_html(entity):
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return '<html><script type="application/json" id="__NEXT_DATA__">' + json.dumps(data) + '</script></html>'


def public_entity(kind="playlist"):
    return {"id": "abc", "type": kind, "name": "音楽", "subtitle": "Someone Else",
            "trackList": [{"uri": "spotify:track:xyz", "title": "曲", "subtitle": "Artist One,\u00a0Artist Two", "duration": 181_000},
                          {"uri": "spotify:episode:xyz", "title": "Podcast"}],
            "visualIdentity": {"image": [{"url": "https://example.com/cover.jpg"}]}}


def test_public_playlist_keeps_order_and_reports_metadata_limits():
    collection = parse_public_collection(embed_html(public_entity()), "playlist", "abc")
    assert collection.owner == "Someone Else"
    assert len(collection.tracks) == 1
    track = collection.tracks[0]
    assert track.artists == ["Artist One", "Artist Two"]
    assert track.duration_text == "3:01"
    assert track.album == "" and track.cover_url == ""
    assert collection.source_note


def test_public_album_attaches_album_cover():
    collection = parse_public_collection(embed_html(public_entity("album")), "album", "abc")
    assert collection.tracks[0].album == "音楽"
    assert collection.tracks[0].cover_url.endswith("cover.jpg")


def test_public_track_uses_full_duration_and_structured_artists():
    entity = {"id": "abc", "type": "track", "uri": "spotify:track:abc", "title": "曲", "duration": 181_000,
              "artists": [{"name": "Artist, with comma"}], "releaseDate": {"isoString": "2025-01-01T00:00:00Z"}}
    track = parse_public_collection(embed_html(entity), "track", "abc").tracks[0]
    assert track.artists == ["Artist, with comma"]
    assert track.release_date == "2025-01-01"
    assert track.duration_ms == 181_000


def test_missing_or_wrong_public_entity_fails_clearly():
    for html in ["<html>not found</html>", embed_html(public_entity("album"))]:
        with pytest.raises(SpotifyError):
            parse_public_collection(html, "playlist", "abc")


def test_api_denied_third_party_playlist_falls_back_to_public(monkeypatch):
    client = SpotifyClient("")
    client._token = {"refresh_token": "test"}
    def denied(*_args):
        raise SpotifyError("403")
    monkeypatch.setattr(client, "_get_api_collection", denied)
    monkeypatch.setattr(client, "get_public_collection", lambda kind, resource_id: parse_public_collection(embed_html(public_entity()), kind, resource_id))
    assert client.get_collection("spotify:playlist:abc").tracks[0].name == "曲"


def test_api_single_album_paginates_and_preserves_track_numbers(monkeypatch):
    client = SpotifyClient("")
    calls = []
    def get(path, params):
        calls.append((path, params))
        track = {"id": "xyz", "name": "曲", "artists": [{"name": "Artist"}], "duration_ms": 181000, "track_number": 4, "disc_number": 2}
        if path == "/albums/abc":
            return {"name": "Single", "album_type": "single", "artists": [{"name": "Artist"}], "release_date": "2025-01-01",
                    "tracks": {"items": [track], "next": "next"}}
        return {"items": [{**track, "track_number": 5}], "next": None}
    monkeypatch.setattr(client, "_get", get)
    result = client._get_api_collection("album", "abc", "JP")
    assert result.kind == "single" and len(result.tracks) == 2
    assert result.tracks[1].track_number == 5 and result.tracks[0].disc_number == 2
    assert result.tracks[0].album == "Single"
    assert calls[-1][1]["offset"] == 1


@pytest.mark.parametrize("value", ["https://youtu.be/abcdefghijk?t=10", "https://www.youtube.com/watch?v=abcdefghijk&list=playlist", "https://music.youtube.com/watch?v=abcdefghijk", "https://youtube.com/shorts/abcdefghijk"])
def test_manual_video_url_is_canonicalized(value):
    assert canonical_youtube_url(value) == "https://www.youtube.com/watch?v=abcdefghijk"


@pytest.mark.parametrize("value", ["https://youtube.com.evil.example/watch?v=abcdefghijk", "file:///tmp/video", "https://youtube.com/playlist?list=abc", "https://youtube.com/watch?v=abc", "https://user@youtube.com/watch?v=abcdefghijk"])
def test_manual_video_rejects_non_video_urls(value):
    with pytest.raises(ValueError):
        canonical_youtube_url(value)


@pytest.fixture
def service(tmp_path):
    events = []
    backend = Backend(lambda event, **data: events.append({"event": event, **data}), ConfigStore(tmp_path))
    backend.playlist = Playlist("abc", "Test", "Owner", "", "", tracks=[sample_track(), sample_track()])
    return backend, events


def run_command(backend, command):
    backend.dispatch(command)
    if backend.worker:
        backend.worker.join(timeout=5)
        assert not backend.worker.is_alive()


def test_manual_correction_can_recover_failed_match(service, monkeypatch):
    backend, events = service
    track = backend.playlist.tracks[0]
    track.excluded = True
    candidate = SearchCandidate("https://www.youtube.com/watch?v=abcdefghijk", "Correct", "Artist", 180, 1.2)
    monkeypatch.setattr(AudioDownloader, "candidate_from_url", lambda *_: candidate)
    monkeypatch.setattr(AudioDownloader, "fetch_thumbnail", lambda *_: b"")
    run_command(backend, {"action": "correct", "index": 0, "url": candidate.url})
    assert track.selected_video_url == candidate.url and not track.excluded
    assert track.status == "手動指定" and track.replace_existing
    assert backend.mapping_ready and events[-1]["event"] == "idle"


def test_correction_search_returns_ranked_candidates(service, monkeypatch):
    backend, events = service
    found = [
        SearchCandidate("https://www.youtube.com/watch?v=aaaaaaaaaaa", "Best", "Artist", 181, 1.7, title_score=0.95),
        SearchCandidate("https://www.youtube.com/watch?v=bbbbbbbbbbb", "Cover", "Someone", 181, 0.2, title_score=0.3),
    ]
    seen = {}
    def search_candidates(_self, _track, query="", limit=6):
        seen["query"] = query
        return found
    monkeypatch.setattr(AudioDownloader, "search_candidates", search_candidates)
    monkeypatch.setattr(AudioDownloader, "fetch_thumbnail", lambda *_: b"")
    run_command(backend, {"action": "correct_search", "index": 0, "query": "魔性の女 椎名林檎"})
    assert seen["query"] == "魔性の女 椎名林檎"
    payload = next(e for e in events if e["event"] == "correction_candidates")
    assert payload["index"] == 0
    assert [c["url"][-11:] for c in payload["candidates"]] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert payload["candidates"][0]["duration_text"] == "3:01"
    assert not backend.busy


def test_correction_search_rejects_bad_index(service):
    backend, _ = service
    run_command(backend, {"action": "correct_search", "index": 99, "query": ""})
    assert not backend.busy


def test_failed_correction_preserves_previous_match(service, monkeypatch):
    backend, events = service
    track = backend.playlist.tracks[0]
    track.selected_video_url = "previous"
    def fail(*_args):
        raise ValueError("bad URL")
    monkeypatch.setattr(AudioDownloader, "candidate_from_url", fail)
    run_command(backend, {"action": "correct", "index": 0, "url": "invalid"})
    assert track.selected_video_url == "previous"
    assert any(e["event"] == "error" for e in events)
    assert not backend.busy


def test_cancelled_search_does_not_enable_download_and_clears_waiting(service, monkeypatch):
    backend, events = service
    def cancel(*_args):
        backend.cancel.set()
        raise DownloadCancelled()
    monkeypatch.setattr(AudioDownloader, "search", cancel)
    run_command(backend, {"action": "match"})
    assert not backend.mapping_ready
    assert backend.playlist.tracks[1].status == "未検索"
    assert events[-1] == {"event": "idle", "mapping_ready": False}


def test_download_only_includes_nonexcluded_candidates(service, monkeypatch):
    backend, events = service
    backend.mapping_ready = True
    for track in backend.playlist.tracks:
        track.selected_video_url = "candidate"
    backend.playlist.tracks[1].excluded = True
    downloaded = []
    monkeypatch.setattr(backend_module, "ffmpeg_available", lambda *_: True)
    def download(_self, _playlist, track):
        downloaded.append(track)
        return None, False
    monkeypatch.setattr(AudioDownloader, "download_track", download)
    run_command(backend, {"action": "download"})
    assert downloaded == [backend.playlist.tracks[0]]
    assert backend.playlist.tracks[0].status == "保存済み"


def test_config_validation_does_not_save_invalid_data(service):
    backend, _ = service
    invalid = asdict(AppConfig(market="invalid"))
    with pytest.raises(ValueError):
        backend.dispatch({"action": "configure", "config": invalid})
    assert not backend.store.path.exists()


def test_corrected_file_is_replaced_only_after_success(tmp_path, monkeypatch):
    track = sample_track()
    track.selected_video_url = "https://www.youtube.com/watch?v=abcdefghijk"
    track.replace_existing = True
    playlist = Playlist("abc", "Test", "Owner", "", "", tracks=[track])
    downloader = AudioDownloader(AppConfig(output_dir=str(tmp_path)), threading.Event(), lambda *_: None)
    target = downloader.target_path(playlist, track)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old audio")
    class FakeYdl:
        def __init__(self, options): self.options = options
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def download(self, _urls):
            from pathlib import Path
            Path(self.options["outtmpl"].replace("%(ext)s", "mp3")).write_bytes(b"corrected audio")
    monkeypatch.setattr("playlist_audio_saver.downloader.yt_dlp.YoutubeDL", FakeYdl)
    def fail_tags(*_args): raise OSError("tag error")
    monkeypatch.setattr(downloader, "write_tags", fail_tags)
    with pytest.raises(OSError):
        downloader.download_track(playlist, track)
    assert target.read_bytes() == b"old audio"
    monkeypatch.setattr(downloader, "write_tags", lambda *_: None)
    _, skipped = downloader.download_track(playlist, track)
    assert not skipped and target.read_bytes() == b"corrected audio"
    assert not track.replace_existing


def test_production_transport_serializes_collection_kind(monkeypatch):
    import io
    import sys
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"action":"shutdown"}\n'))
    class FakeBackend:
        cancel = threading.Event()
        worker = None
        def __init__(self, emit): self.emit = emit
        def state(self): self.emit("playlist", kind="album", name="日本語")
    monkeypatch.setattr(backend_module, "Backend", FakeBackend)
    backend_module.main()
    assert json.loads(output.getvalue()) == {"event": "playlist", "kind": "album", "name": "日本語"}
