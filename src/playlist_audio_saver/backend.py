"""Line-delimited JSON transport for the native Windows desktop client.

Only this process owns credentials and mutable playlist state. stdout is reserved
for protocol messages; third-party output is redirected to stderr.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import AppConfig, ConfigStore
from .downloader import AudioDownloader, DownloadCancelled, SearchCandidate, apply_candidate, ffmpeg_available
from .models import Playlist, Track
from .spotify import SpotifyClient, SpotifyError, spotify_resource
from .updater import GitHubUpdater, UpdateCancelled, launch_update_and_restart


def candidate_status(track: Track, candidate: SearchCandidate) -> str:
    expected = track.duration_ms / 1000
    difference = abs(candidate.duration - expected) if candidate.duration and expected else 0
    uncertain = (candidate.score < 0.85 or candidate.title_score < 0.6
                 or difference > max(15, expected * 0.08))
    return "要確認" if uncertain else "対応候補"


def duration_text(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}" if total else "--:--"


def track_data(track: Track) -> dict[str, Any]:
    return {**asdict(track), "artist_text": track.artist_text, "duration_text": track.duration_text,
            "youtube_duration_text": track.youtube_duration_text}


def validated_config(raw: dict[str, Any]) -> AppConfig:
    config = AppConfig(**raw)
    config.client_id = "".join(config.client_id.split())
    if config.client_id and (len(config.client_id) < 16 or not config.client_id.isalnum()):
        raise ValueError("Spotify Client IDの形式を確認してください。")
    config.market = config.market.strip().upper()
    if len(config.market) != 2 or not config.market.isascii() or not config.market.isalpha():
        raise ValueError("市場はJPのような2文字の国コードを入力してください。")
    if not 0 <= int(config.audio_quality) <= 9 or not 1 <= int(config.search_results) <= 10:
        raise ValueError("音質は0〜9、検索候補数は1〜10で指定してください。")
    if not 1024 <= int(config.redirect_port) <= 65535:
        raise ValueError("認証ポートは1024〜65535で指定してください。")
    config.audio_quality = str(config.audio_quality)
    config.search_results = int(config.search_results)
    config.redirect_port = int(config.redirect_port)
    config.output_dir = config.output_dir.strip()
    config.ffmpeg_path = config.ffmpeg_path.strip().strip('"')
    return config


class Backend:
    def __init__(self, emit: Callable[..., None], store: ConfigStore | None = None) -> None:
        self.emit = emit
        self.store = store or ConfigStore()
        self.config = self.store.load()
        self.spotify = SpotifyClient(self.config.client_id, self.config.redirect_port)
        self.playlist: Playlist | None = None
        self.mapping_ready = False
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.release = None
        self.staging: Path | None = None

    def state(self) -> None:
        self.emit("state", config=asdict(self.config), connected=self.spotify.is_connected,
                  output_dir=str(self.config.resolved_output_dir), version=__version__,
                  ffmpeg=ffmpeg_available(self.config.ffmpeg_path), packaged=bool(getattr(sys, "frozen", False)))

    def dispatch(self, command: dict[str, Any]) -> None:
        action = command.get("action")
        if action == "cancel":
            self.cancel.set()
            return
        if self.busy:
            self.emit("error", message="現在の処理が終わるまでお待ちください。")
            return
        if action == "configure":
            config = validated_config(command["config"])
            self.store.save(config)
            if (config.client_id, config.redirect_port) != (self.config.client_id, self.config.redirect_port):
                self.spotify = SpotifyClient(config.client_id, config.redirect_port)
            self.config = config
            self.state()
            self.emit("configured")
        elif action == "disconnect":
            self.spotify.disconnect()
            self.state()
            self.emit("notice", message="Spotifyの接続を解除しました。")
        elif action == "exclude":
            if not self.playlist or not self.mapping_ready:
                raise ValueError("先に対応を検索してください。")
            index = int(command["index"])
            if not 0 <= index < len(self.playlist.tracks):
                raise ValueError("曲が見つかりません。")
            track = self.playlist.tracks[index]
            if not track.selected_video_url:
                raise ValueError("この曲には候補がありません。")
            track.excluded = not track.excluded
            self.emit("track", index=index, track=track_data(track))
        elif action in {"load", "connect", "match", "correct", "correct_search", "download",
                        "check_update", "download_update"}:
            self.cancel.clear()
            self.busy = True
            self.emit("busy", operation=action,
                      cancellable=action in {"match", "download", "download_update"})

            def run() -> None:
                try:
                    getattr(self, "_" + action)(command)
                except (DownloadCancelled, UpdateCancelled):
                    self.emit("notice", message="処理を停止しました。")
                except Exception as exc:
                    self.emit("error", message=str(exc), silent=bool(command.get("silent")))
                finally:
                    self.busy = False
                    self.emit("idle", mapping_ready=self.mapping_ready)

            self.worker = threading.Thread(target=run, daemon=True)
            self.worker.start()
        elif action == "apply_update":
            if not self.staging:
                raise ValueError("更新ファイルがありません。")
            # The native host supplies its own path and PID when starting this child.
            host = os.environ.get("PLAYLIST_SAVER_HOST")
            pid = os.environ.get("PLAYLIST_SAVER_HOST_PID")
            launch_update_and_restart(self.staging, host_executable=Path(host) if host else None,
                                      host_pid=int(pid) if pid else None)
            self.emit("exit_for_update")
        else:
            raise ValueError("不明な操作です。")

    def _load(self, command: dict[str, Any]) -> None:
        value = str(command.get("url", "")).strip()
        spotify_resource(value)
        try:
            playlist = self.spotify.get_collection(value, self.config.market)
        except SpotifyError:
            if not self.config.client_id or self.spotify.is_connected:
                raise
            self.emit("progress", percent=0, message="ブラウザーでSpotifyへの接続を許可してください。")
            self.spotify.authorize()
            self.state()
            playlist = self.spotify.get_collection(value, self.config.market)
        self.playlist = playlist
        self.mapping_ready = False
        self.emit("playlist", name=playlist.name, owner=playlist.owner, kind=playlist.kind, source_note=playlist.source_note,
                  tracks=[track_data(track) for track in playlist.tracks])
        self.emit("notice", message="対応を検索して、YouTubeの候補を確認しましょう。" if playlist.tracks else "このプレイリストには曲がありません。")

    def _connect(self, _command: dict[str, Any]) -> None:
        self.spotify.authorize()
        self.state()
        self.emit("notice", message="Spotifyに接続しました。")

    def _match(self, _command: dict[str, Any]) -> None:
        if not self.playlist or not self.playlist.tracks:
            raise ValueError("プレイリストを読み込んでください。")
        self.mapping_ready = False
        tracks = self.playlist.tracks
        downloader = AudioDownloader(self.config, self.cancel, lambda *_: None)
        for index, track in enumerate(tracks):
            track.selected_video_url = track.selected_video_title = track.selected_video_uploader = ""
            track.youtube_thumbnail_url = ""
            track.selected_video_duration = track.match_score = 0
            track.excluded = False
            track.status = "検索待ち"
            self.emit("track", index=index, track=track_data(track))
        matched = failed = 0
        for index, track in enumerate(tracks):
            if self.cancel.is_set():
                break
            track.status = "検索中"
            self.emit("track", index=index, track=track_data(track))
            try:
                candidate = downloader.search(track)
                apply_candidate(track, candidate)
                thumbnail = downloader.fetch_thumbnail(candidate.thumbnail_url)
                track.status = candidate_status(track, candidate)
                matched += 1
                self.emit("track", index=index, track=track_data(track),
                          thumbnail=base64.b64encode(thumbnail).decode("ascii") if thumbnail else "")
            except DownloadCancelled:
                track.status = "停止"
                self.emit("track", index=index, track=track_data(track))
                break
            except Exception as exc:
                failed += 1
                track.excluded = True
                track.status = "候補なし"
                self.emit("track", index=index, track=track_data(track), detail=str(exc))
            self.emit("progress", percent=(index + 1) / len(tracks) * 100, message=f"対応を検索中  ·  {index + 1} / {len(tracks)} 曲")
        if self.cancel.is_set():
            for index, track in enumerate(tracks):
                if track.status == "検索待ち":
                    track.status = "未検索"
                    self.emit("track", index=index, track=track_data(track))
        self.mapping_ready = not self.cancel.is_set() and matched > 0
        self.emit("notice", message="検索を停止しました。再検索できます。" if self.cancel.is_set() else
                  f"検索完了  ·  候補あり {matched} 曲 / 候補なし {failed} 曲。対応を確認して保存してください。")

    def _download(self, _command: dict[str, Any]) -> None:
        if not self.playlist or not self.mapping_ready:
            raise ValueError("先に対応を検索してください。")
        tracks = [(i, t) for i, t in enumerate(self.playlist.tracks) if t.selected_video_url and not t.excluded]
        if not tracks:
            raise ValueError("保存対象がありません。")
        if not ffmpeg_available(self.config.ffmpeg_path):
            raise ValueError("FFmpegが見つかりません。設定からffmpeg.exeを選択してください。")
        completed = skipped = failed = 0
        errors = []

        def progress(ratio: float | None, label: str) -> None:
            self.emit("progress", percent=(completed + (ratio or 0)) / len(tracks) * 100, message=label)

        downloader = AudioDownloader(self.config, self.cancel, progress)
        for index, track in tracks:
            if self.cancel.is_set():
                break
            track.status = "保存中"
            self.emit("track", index=index, track=track_data(track))
            try:
                _, was_skipped = downloader.download_track(self.playlist, track)
                skipped += int(was_skipped)
                track.status = "既存スキップ" if was_skipped else "保存済み"
            except DownloadCancelled:
                track.status = "停止"
                self.emit("track", index=index, track=track_data(track))
                break
            except Exception as exc:
                failed += 1
                track.status = "失敗"
                errors.append(f"{track.name}: {str(exc)[:350]}")
                self.emit("track", index=index, track=track_data(track), detail=str(exc))
            completed += 1
            self.emit("track", index=index, track=track_data(track))
            self.emit("progress", percent=completed / len(tracks) * 100, message=f"保存中  ·  {completed} / {len(tracks)} 曲")
        self.emit("notice", message=f"{'停止' if self.cancel.is_set() else '保存完了'}  ·  処理 {completed} 曲 / スキップ {skipped} 曲 / 失敗 {failed} 曲",
                  detail="\n\n".join(errors[:5]))

    def _correct(self, command: dict[str, Any]) -> None:
        index = int(command["index"])
        if not self.playlist or not 0 <= index < len(self.playlist.tracks):
            raise ValueError("曲が見つかりません。")
        track = self.playlist.tracks[index]
        downloader = AudioDownloader(self.config, self.cancel, lambda *_: None)
        candidate = downloader.candidate_from_url(track, str(command.get("url", "")))
        thumbnail = downloader.fetch_thumbnail(candidate.thumbnail_url)
        apply_candidate(track, candidate)
        track.excluded = False
        track.replace_existing = True
        track.status = "手動指定"
        self.mapping_ready = True
        self.emit("track", index=index, track=track_data(track), thumbnail=base64.b64encode(thumbnail).decode("ascii") if thumbnail else "", detail="")
        self.emit("notice", message=f"「{track.name}」の候補を変更しました。動画を確認してから保存してください。")

    def _correct_search(self, command: dict[str, Any]) -> None:
        index = int(command["index"])
        if not self.playlist or not 0 <= index < len(self.playlist.tracks):
            raise ValueError("曲が見つかりません。")
        track = self.playlist.tracks[index]
        downloader = AudioDownloader(self.config, self.cancel, lambda *_: None)
        query = str(command.get("query", "")).strip()
        candidates = downloader.search_candidates(track, query, limit=6)
        results = []
        for candidate in candidates:
            if self.cancel.is_set():
                raise DownloadCancelled("キャンセルしました。")
            thumbnail = downloader.fetch_thumbnail(candidate.thumbnail_url)
            results.append({
                "url": candidate.url,
                "title": candidate.title,
                "uploader": candidate.uploader,
                "duration_text": duration_text(candidate.duration),
                "score": round(candidate.score, 3),
                "title_score": round(candidate.title_score, 3),
                "thumbnail": base64.b64encode(thumbnail).decode("ascii") if thumbnail else "",
            })
        self.emit("correction_candidates", index=index, query=query, candidates=results)

    def _check_update(self, command: dict[str, Any]) -> None:
        self.release = GitHubUpdater(__version__).check()
        self.emit("update", release=asdict(self.release) if self.release else None, silent=bool(command.get("silent")))

    def _download_update(self, _command: dict[str, Any]) -> None:
        if not self.release:
            raise ValueError("先に更新を確認してください。")
        self.staging = GitHubUpdater(__version__).download(
            self.release, lambda ratio: self.emit("progress", percent=ratio * 100, message=f"更新をダウンロード中 {ratio * 100:.0f}%"), self.cancel)
        self.emit("update_ready")


def main() -> None:
    output = sys.stdout
    sys.stdout = sys.stderr
    lock = threading.Lock()

    def emit(event_type: str, **payload: Any) -> None:
        with lock:
            output.write(json.dumps({"event": event_type, **payload}, ensure_ascii=True) + "\n")
            output.flush()

    backend = Backend(emit)
    backend.state()
    for line in sys.stdin:
        try:
            command = json.loads(line)
            if command.get("action") == "shutdown":
                backend.cancel.set()
                break
            backend.dispatch(command)
        except Exception as exc:
            emit("error", message=str(exc))
    backend.cancel.set()
    # Keep the child alive until its worker exits, so the native host can kill
    # the entire process tree (including FFmpeg) if graceful shutdown times out.
    if backend.worker:
        backend.worker.join()


if __name__ == "__main__":
    main()
