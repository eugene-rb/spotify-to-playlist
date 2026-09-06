from dataclasses import replace

from playlist_audio_saver.corrections import CorrectionStore
from test_core import sample_track


def test_remembers_and_looks_up_by_spotify_id(tmp_path):
    store = CorrectionStore(tmp_path)
    track = replace(sample_track(), spotify_id="track-1", name="GTA", artists=["chaplin"])
    store.remember(track, video_url="https://www.youtube.com/watch?v=abcdefghijk",
                   video_title="GTA", video_uploader="Chaplin")

    reloaded = CorrectionStore(tmp_path)
    hit = reloaded.lookup(replace(sample_track(), spotify_id="track-1", name="different", artists=["x"]))
    assert hit is not None and hit.video_url.endswith("abcdefghijk")


def test_looks_up_by_title_and_artist_when_id_differs(tmp_path):
    store = CorrectionStore(tmp_path)
    store.remember(replace(sample_track(), spotify_id="old", name="Pressure", artists=["Tone October"]),
                   video_url="https://www.youtube.com/watch?v=abcdefghijk", video_title="pressure", video_uploader="tone october")
    hit = store.lookup(replace(sample_track(), spotify_id="new", name="pressure", artists=["tone october"]))
    assert hit is not None and hit.video_uploader == "tone october"


def test_latest_correction_wins(tmp_path):
    store = CorrectionStore(tmp_path)
    track = replace(sample_track(), spotify_id="t", name="Song", artists=["A"])
    store.remember(track, video_url="https://www.youtube.com/watch?v=aaaaaaaaaaa", video_title="", video_uploader="one")
    store.remember(track, video_url="https://www.youtube.com/watch?v=bbbbbbbbbbb", video_title="", video_uploader="two")
    assert store.count == 1
    assert store.lookup(track).video_url.endswith("bbbbbbbbbbb")


def test_preferred_uploaders_are_scoped_to_the_artist(tmp_path):
    store = CorrectionStore(tmp_path)
    store.remember(replace(sample_track(), spotify_id="1", name="GTA", artists=["chaplin"]),
                   video_url="https://www.youtube.com/watch?v=aaaaaaaaaaa", video_title="", video_uploader="Chaplin")
    store.remember(replace(sample_track(), spotify_id="2", name="x", artists=["Someone Else"]),
                   video_url="https://www.youtube.com/watch?v=bbbbbbbbbbb", video_title="", video_uploader="Other Channel")
    other_track = replace(sample_track(), spotify_id="3", name="Poison", artists=["Chaplin"])
    assert store.preferred_uploaders(other_track) == frozenset({"chaplin"})
    assert store.preferred_uploaders(replace(sample_track(), artists=["Nobody"])) == frozenset()


def test_corrupt_file_is_ignored(tmp_path):
    (tmp_path / "corrections.json").write_text("{ not json", encoding="utf-8")
    store = CorrectionStore(tmp_path)
    assert store.count == 0
    store.remember(replace(sample_track(), spotify_id="t"), video_url="https://www.youtube.com/watch?v=abcdefghijk",
                   video_title="", video_uploader="")
    assert store.count == 1


def test_clear_removes_everything(tmp_path):
    store = CorrectionStore(tmp_path)
    store.remember(replace(sample_track(), spotify_id="t"), video_url="https://www.youtube.com/watch?v=abcdefghijk",
                   video_title="", video_uploader="")
    store.clear()
    assert store.count == 0
    assert not (tmp_path / "corrections.json").exists()
    assert CorrectionStore(tmp_path).count == 0
