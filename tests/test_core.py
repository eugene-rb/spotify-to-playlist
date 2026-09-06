import threading
from dataclasses import replace

from playlist_audio_saver.backend import candidate_status
from playlist_audio_saver.config import AppConfig
from playlist_audio_saver.downloader import (
    AudioDownloader,
    DownloadError,
    SearchCandidate,
    apply_candidate,
    assess_candidate,
    safe_filename,
    score_candidate,
)
from playlist_audio_saver.models import Playlist, Track
from playlist_audio_saver.spotify import _parse_track, playlist_id_from_url
from playlist_audio_saver.updater import GitHubUpdater, newer_version


def sample_track() -> Track:
    return Track(
        position=1,
        spotify_id="abc",
        name="Example Song",
        artists=["Sample Artist"],
        album="Example Album",
        album_artists=["Sample Artist"],
        release_date="2025-01-02",
        track_number=1,
        disc_number=1,
        duration_ms=180_000,
        explicit=False,
        isrc="JPABC2500001",
        spotify_url="https://open.spotify.com/track/abc",
        cover_url="https://example.com/cover.jpg",
    )


def test_playlist_url_and_uri_are_supported() -> None:
    expected = "37i9dQZF1DXcBWIGoYBM5M"
    assert playlist_id_from_url(f"https://open.spotify.com/playlist/{expected}?si=123") == expected
    assert playlist_id_from_url(f"spotify:playlist:{expected}") == expected


def test_non_playlist_url_is_rejected() -> None:
    try:
        playlist_id_from_url("https://open.spotify.com/album/abc")
    except ValueError:
        pass
    else:
        raise AssertionError("album URL must be rejected")


def test_windows_filename_is_sanitized() -> None:
    assert safe_filename('A/B: C? <D> "E"') == "A_B_ C_ _D_ _E_"
    assert safe_filename("CON") == "_CON"


def test_target_path_uses_spotify_title_only(tmp_path) -> None:
    downloader = AudioDownloader(AppConfig(output_dir=str(tmp_path)), threading.Event(), lambda *_: None)
    track = replace(sample_track(), position=4, name="Example Song")
    playlist = Playlist("id", "My Mix", "Owner", "", "", tracks=[track])
    target = downloader.target_path(playlist, track)
    assert target.name == "Example Song.mp3"
    assert target.parent.name == "My Mix"


def test_target_path_disambiguates_duplicate_titles(tmp_path) -> None:
    downloader = AudioDownloader(AppConfig(output_dir=str(tmp_path)), threading.Event(), lambda *_: None)
    first = replace(sample_track(), position=1, spotify_id="a", name="Intro")
    second = replace(sample_track(), position=2, spotify_id="b", name="Intro")
    playlist = Playlist("id", "Mix", "Owner", "", "", tracks=[first, second])
    assert downloader.target_path(playlist, first).name == "Intro.mp3"
    assert downloader.target_path(playlist, second).name == "Intro (2).mp3"


def test_matching_track_beats_wrong_live_version() -> None:
    track = sample_track()
    official = {"title": "Sample Artist - Example Song (Official Audio)", "uploader": "Sample Artist", "duration": 181}
    wrong = {"title": "Example Song live cover", "uploader": "Other", "duration": 245}
    assert score_candidate(track, official) > score_candidate(track, wrong)


def test_wrong_song_by_right_artist_is_not_a_confident_match() -> None:
    track = replace(sample_track(), name="魔性の女", artists=["椎名林檎"], duration_ms=210_000)
    right_artist_wrong_song = {"title": "椎名林檎 - 公然の秘密", "uploader": "椎名林檎", "duration": 205}
    real = {"title": "椎名林檎 - 魔性の女", "uploader": "椎名林檎", "duration": 209}
    wrong_score, wrong_title = assess_candidate(track, right_artist_wrong_song)
    real_score, real_title = assess_candidate(track, real)
    assert real_score > wrong_score
    assert wrong_title < 0.4
    # A different song by the right artist must never look confidently correct.
    assert candidate_status(track, _candidate(right_artist_wrong_song, wrong_score, wrong_title)) == "要確認"
    assert candidate_status(track, _candidate(real, real_score, real_title)) == "対応候補"


def test_uploader_being_the_artist_or_topic_channel_wins() -> None:
    track = replace(sample_track(), name="夜行", artists=["ヨルシカ"], duration_ms=240_000)
    base = {"title": "ヨルシカ - 夜行", "duration": 240}
    by_artist = assess_candidate(track, {**base, "uploader": "ヨルシカ"})[0]
    by_topic = assess_candidate(track, {**base, "uploader": "ヨルシカ - Topic"})[0]
    by_random = assess_candidate(track, {**base, "uploader": "music uploads 24h"})[0]
    assert by_artist > by_random
    assert by_topic > by_random
    # A "- Topic" channel for a *different* artist gets no authority bonus.
    other_topic = assess_candidate(track, {**base, "uploader": "someone else - Topic"})[0]
    assert other_topic < by_topic


def test_lyric_and_cover_uploads_rank_below_official() -> None:
    track = replace(sample_track(), name="花束", artists=["back number"], duration_ms=300_000)
    official = assess_candidate(track, {"title": "back number - 花束", "uploader": "back number", "duration": 300})[0]
    lyric = assess_candidate(track, {"title": "back number - 花束 (Lyric Video)", "uploader": "back number", "duration": 300})[0]
    cover = assess_candidate(track, {"title": "花束 / back number 歌ってみた", "uploader": "cover channel", "duration": 300})[0]
    assert official > lyric > cover


def test_search_uses_ranked_results_and_retries(monkeypatch) -> None:
    track = sample_track()
    downloader = AudioDownloader(AppConfig(), threading.Event(), lambda *_: None)
    entries = [
        {"id": "a" * 11, "title": "Sample Artist - Example Song", "uploader": "Sample Artist", "duration": 181},
        {"id": "b" * 11, "title": "Example Song 歌ってみた", "uploader": "someone", "duration": 181},
    ]
    calls = {"n": 0}

    class FakeYdl:
        def __init__(self, *_a, **_k): ...
        def __enter__(self): return self
        def __exit__(self, *_): ...
        def extract_info(self, _target, download=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            return {"entries": entries}

    monkeypatch.setattr("playlist_audio_saver.downloader.yt_dlp.YoutubeDL", FakeYdl)
    monkeypatch.setattr(downloader.cancel_event, "wait", lambda *_a, **_k: False)
    best = downloader.search(track)
    assert calls["n"] == 2
    assert best.url.endswith("a" * 11)

    ranked = downloader.search_candidates(track, limit=5)
    assert [candidate.url[-11:] for candidate in ranked] == ["a" * 11, "b" * 11]


def test_search_relaxes_the_query_when_youtube_returns_nothing(monkeypatch) -> None:
    # "魔性の女A": some YouTube queries with a trailing Latin letter return zero
    # results, so the ranker must fall back to a looser query instead of 候補なし.
    track = replace(sample_track(), name="魔性の女A", artists=["紫今"], duration_ms=222_000)
    downloader = AudioDownloader(AppConfig(), threading.Event(), lambda *_: None)
    hit = {"title": "紫今 - 魔性の女A", "uploader": "紫今", "id": "c" * 11, "duration": 222}
    seen: list[str] = []

    class FakeYdl:
        def __init__(self, *_a, **_k): ...
        def __enter__(self): return self
        def __exit__(self, *_): ...
        def extract_info(self, target, download=False):
            seen.append(target)
            return {"entries": [hit]} if "魔性の女A" not in target else {"entries": []}

    monkeypatch.setattr("playlist_audio_saver.downloader.yt_dlp.YoutubeDL", FakeYdl)
    best = downloader.search(track)
    assert best.url.endswith("c" * 11)
    assert any("魔性の女A" in query for query in seen)      # tried the exact title first
    assert any(query.endswith("魔性の女") for query in seen)  # then the relaxed one


def test_search_raises_when_every_attempt_fails(monkeypatch) -> None:
    downloader = AudioDownloader(AppConfig(), threading.Event(), lambda *_: None)

    class FailingYdl:
        def __init__(self, *_a, **_k): ...
        def __enter__(self): return self
        def __exit__(self, *_): ...
        def extract_info(self, *_a, **_k): raise RuntimeError("network down")

    monkeypatch.setattr("playlist_audio_saver.downloader.yt_dlp.YoutubeDL", FailingYdl)
    monkeypatch.setattr(downloader.cancel_event, "wait", lambda *_a, **_k: False)
    try:
        downloader.search(sample_track())
    except DownloadError as exc:
        assert "network down" in str(exc)
    else:
        raise AssertionError("search must raise after exhausting retries")


def _candidate(entry: dict, score: float, title_score: float) -> SearchCandidate:
    return SearchCandidate(
        url="https://www.youtube.com/watch?v=" + "x" * 11,
        title=str(entry["title"]),
        uploader=str(entry["uploader"]),
        duration=float(entry["duration"]),
        score=score,
        title_score=title_score,
    )


def test_selected_youtube_candidate_is_saved_on_track() -> None:
    track = sample_track()
    candidate = SearchCandidate(
        url="https://www.youtube.com/watch?v=video",
        title="Sample Artist - Example Song",
        uploader="Sample Artist",
        duration=181,
        score=1.2,
        thumbnail_url="https://i.ytimg.com/vi/video/mqdefault.jpg",
    )
    apply_candidate(track, candidate)
    assert track.selected_video_url == candidate.url
    assert track.youtube_thumbnail_url == candidate.thumbnail_url
    assert track.youtube_duration_text == "3:01"


def test_spotify_track_parser() -> None:
    data = {
        "id": "abc",
        "type": "track",
        "name": "Example Song",
        "artists": [{"name": "Sample Artist"}],
        "album": {
            "name": "Example Album",
            "artists": [{"name": "Sample Artist"}],
            "release_date": "2025-01-02",
            "images": [{"url": "https://example.com/cover.jpg"}],
        },
        "track_number": 3,
        "disc_number": 1,
        "duration_ms": 180000,
        "external_ids": {"isrc": "JPABC2500001"},
        "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
    }
    result = _parse_track(data, 7)
    assert result is not None
    assert result.position == 7
    assert result.track_number == 3
    assert result.artist_text == "Sample Artist"


def test_updater_compares_semantic_versions() -> None:
    assert newer_version("v0.3.0", "0.2.0")
    assert newer_version("1.0.0", "0.9.9")
    assert not newer_version("v0.2.0", "0.2.0")
    assert not newer_version("not-a-version", "0.2.0")


def test_updater_reads_release_manifest() -> None:
    class Response:
        status_code = 200
        ok = True

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "tag_name": "v0.3.0",
                "sha256": "a" * 64,
                "size": 123,
                "notes": "changes",
            }

    class Session:
        @staticmethod
        def get(*_args: object, **_kwargs: object) -> Response:
            return Response()

    updater = GitHubUpdater("0.2.0", "owner/repository")
    updater.session = Session()  # type: ignore[assignment]
    release = updater.check()
    assert release is not None
    assert release.version == "0.3.0"
    assert release.download_url.endswith("/v0.3.0/PlaylistAudioSaver-win64.zip")
