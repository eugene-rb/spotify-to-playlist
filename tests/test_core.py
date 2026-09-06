import threading
from dataclasses import replace

from playlist_audio_saver.config import AppConfig
from playlist_audio_saver.downloader import (
    AudioDownloader,
    SearchCandidate,
    apply_candidate,
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
