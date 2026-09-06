from __future__ import annotations

import ctypes
import io
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from PIL import Image, ImageTk

from . import __version__
from .config import AppConfig, ConfigStore
from .downloader import (
    AudioDownloader,
    DownloadCancelled,
    SearchCandidate,
    apply_candidate,
    ffmpeg_available,
)
from .models import Playlist, Track
from .spotify import SpotifyClient
from .updater import GitHubUpdater, ReleaseInfo, UpdateCancelled, launch_update_and_restart


BG = "#0f172a"
PANEL = "#111827"
PANEL_2 = "#1f2937"
TEXT = "#f8fafc"
MUTED = "#94a3b8"
ACCENT = "#22c55e"
ACCENT_ACTIVE = "#16a34a"
DANGER = "#ef4444"
BORDER = "#334155"


class PlaylistAudioSaverApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Playlist Audio Saver")
        self.geometry("1120x720")
        self.minsize(850, 590)
        self.configure(bg=BG)
        self.config_store = ConfigStore()
        self.config_data = self.config_store.load()
        self.spotify = SpotifyClient(self.config_data.client_id, self.config_data.redirect_port)
        self.playlist: Playlist | None = None
        self.cancel_event = threading.Event()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.mapping_ready = False
        self.update_checking = False
        self.thumbnail_images: dict[str, ImageTk.PhotoImage | tk.PhotoImage] = {}
        self._build_styles()
        self._build_ui()
        self.after(80, self._drain_events)
        if self.config_data.check_updates:
            self.after(1500, lambda: self._check_for_updates(silent=True))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 21), foreground=TEXT)
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10), foreground=MUTED)
        style.configure(
            "TButton", background=PANEL_2, foreground=TEXT, bordercolor=BORDER,
            padding=(14, 8), relief="flat",
        )
        style.map("TButton", background=[("active", BORDER), ("disabled", PANEL)])
        style.configure(
            "Accent.TButton", background=ACCENT, foreground="#052e16", bordercolor=ACCENT,
            font=("Segoe UI Semibold", 10),
        )
        style.map("Accent.TButton", background=[("active", ACCENT_ACTIVE), ("disabled", BORDER)])
        style.configure("Danger.TButton", foreground="#fecaca", background="#7f1d1d", bordercolor="#991b1b")
        style.map("Danger.TButton", background=[("active", "#991b1b")])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER, padding=9)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=76, borderwidth=0)
        style.map("Treeview", background=[("selected", "#14532d")])
        style.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED, relief="flat", padding=8)
        style.map("Treeview.Heading", background=[("active", BORDER)])
        style.configure("Horizontal.TProgressbar", troughcolor=PANEL_2, background=ACCENT, bordercolor=PANEL_2)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", PANEL)])

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=24)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Playlist Audio Saver", style="Title.TLabel").pack(side="left")
        self.connection_label = ttk.Label(header, style="Subtitle.TLabel")
        self.connection_label.pack(side="right", padx=(12, 0))
        ttk.Button(header, text="設定", command=self._open_settings).pack(side="right")
        self.update_button = ttk.Button(
            header, text=f"v{__version__}・更新確認", command=self._check_for_updates
        )
        self.update_button.pack(side="right", padx=(0, 8))
        self._update_connection_label()

        ttk.Label(
            outer,
            text="Spotifyの曲情報を使ってYouTubeから一致する音源を探し、タグ付きMP3として保存します。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 18))

        input_frame = ttk.Frame(outer, style="Panel.TFrame", padding=16)
        input_frame.pack(fill="x")
        ttk.Label(input_frame, text="SpotifyプレイリストURL", style="Panel.TLabel").pack(anchor="w", pady=(0, 7))
        row = ttk.Frame(input_frame, style="Panel.TFrame")
        row.pack(fill="x")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(row, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.load_button = ttk.Button(row, text="読み込む", style="Accent.TButton", command=self._load_playlist)
        self.load_button.pack(side="right")

        info_row = ttk.Frame(outer)
        info_row.pack(fill="x", pady=(18, 8))
        self.playlist_label = ttk.Label(info_row, text="プレイリスト未選択", font=("Segoe UI Semibold", 13))
        self.playlist_label.pack(side="left")
        self.count_label = ttk.Label(info_row, text="", style="Muted.TLabel")
        self.count_label.pack(side="left", padx=10)

        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True)
        columns = ("position", "spotify", "artist", "spotify_time", "youtube", "youtube_info", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="動画サムネイル")
        self.tree.column("#0", width=132, minwidth=132, stretch=False)
        headings = {
            "position": "#", "spotify": "Spotify楽曲", "artist": "アーティスト",
            "spotify_time": "時間", "youtube": "YouTube候補", "youtube_info": "投稿者・時間",
            "status": "状態",
        }
        widths = {
            "position": 42, "spotify": 205, "artist": 145, "spotify_time": 55,
            "youtube": 245, "youtube_info": 145, "status": 105,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=widths[column] if column in {"position", "duration"} else 80, stretch=column not in {"position", "duration"})
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", self._open_row_link)
        self.tree.bind("<Motion>", self._tree_motion)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_action_states())
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        progress_row = ttk.Frame(outer)
        progress_row.pack(fill="x", pady=(14, 6))
        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(progress_row, text="準備完了", style="Muted.TLabel", width=28, anchor="e")
        self.progress_label.pack(side="right", padx=(12, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(7, 0))
        ttk.Label(
            actions,
            text="曲名または動画欄をダブルクリックすると元ページを開きます。",
            style="Muted.TLabel",
        ).pack(side="left")
        self.folder_button = ttk.Button(actions, text="保存先を開く", command=self._open_output)
        self.folder_button.pack(side="right", padx=(8, 0))
        self.cancel_button = ttk.Button(actions, text="キャンセル", style="Danger.TButton", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.download_button = ttk.Button(actions, text="確認済みを保存", style="Accent.TButton", command=self._download_all, state="disabled")
        self.download_button.pack(side="right")
        self.match_button = ttk.Button(actions, text="対応を検索", command=self._match_all, state="disabled")
        self.match_button.pack(side="right", padx=(8, 0))
        self.exclude_button = ttk.Button(actions, text="選択曲を除外", command=self._toggle_excluded, state="disabled")
        self.exclude_button.pack(side="right", padx=(8, 0))

    def _run_worker(self, function: Callable[[], None]) -> None:
        threading.Thread(target=function, daemon=True).start()

    def _load_playlist(self) -> None:
        if self.busy:
            return
        value = self.url_var.get().strip()
        if not value:
            messagebox.showinfo("URLが必要です", "SpotifyプレイリストのURLを入力してください。", parent=self)
            return
        if not self.config_data.client_id:
            messagebox.showinfo("Spotify設定", "最初にSpotify Client IDを設定してください。", parent=self)
            self._open_settings()
            return
        self._set_busy(True, cancellable=False)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.progress_label.configure(text="Spotifyから読み込み中")

        def work() -> None:
            try:
                if not self.spotify.is_connected:
                    self.events.put(("auth_required", None))
                    self.spotify.authorize()
                    self.events.put(("auth_changed", None))
                playlist = self.spotify.get_playlist(value, self.config_data.market)
                self.events.put(("playlist", playlist))
            except Exception as exc:
                self.events.put(("error", ("読み込みに失敗しました", str(exc))))
            finally:
                self.events.put(("idle", None))

        self._run_worker(work)

    def _match_all(self) -> None:
        if self.busy or not self.playlist:
            return
        self.cancel_event.clear()
        self.mapping_ready = False
        self.thumbnail_images.clear()
        self._set_busy(True, cancellable=True)
        self.cancel_button.configure(text="検索を停止")
        self.progress.configure(mode="determinate", value=0)
        self.progress_label.configure(text="YouTube対応を検索中")
        playlist = self.playlist
        total = len(playlist.tracks)
        for index, track in enumerate(playlist.tracks):
            track.selected_video_url = ""
            track.selected_video_title = ""
            track.youtube_thumbnail_url = ""
            track.excluded = False
            track.status = "検索待ち"
            self._refresh_row(index)

        def work() -> None:
            matched = failed = 0
            downloader = AudioDownloader(self.config_data, self.cancel_event, lambda *_: None)
            for index, track in enumerate(playlist.tracks):
                if self.cancel_event.is_set():
                    break
                self.events.put(("track_status", (index, "検索中")))
                try:
                    candidate = downloader.search(track)
                    apply_candidate(track, candidate)
                    thumbnail = downloader.fetch_thumbnail(candidate.thumbnail_url)
                    status = _candidate_status(track, candidate)
                    matched += 1
                    self.events.put(("mapping", (index, thumbnail, status)))
                except DownloadCancelled:
                    self.events.put(("track_status", (index, "キャンセル")))
                    break
                except Exception as exc:
                    failed += 1
                    track.excluded = True
                    self.events.put(("mapping_error", (index, str(exc))))
                self.events.put(
                    ("progress", ((index + 1) / max(total, 1) * 100, f"対応検索 {index + 1}/{total} 曲"))
                )
            self.events.put(("mapping_done", (matched, failed, self.cancel_event.is_set())))
            self.events.put(("idle", None))

        self._run_worker(work)

    def _download_all(self) -> None:
        if self.busy or not self.playlist:
            return
        if not self.mapping_ready:
            messagebox.showinfo("対応確認が必要です", "先に「対応を検索」を実行してください。", parent=self)
            return
        selected_tracks = [
            (index, track)
            for index, track in enumerate(self.playlist.tracks)
            if not track.excluded and track.selected_video_url
        ]
        if not selected_tracks:
            messagebox.showinfo("保存対象がありません", "保存する対応を1曲以上残してください。", parent=self)
            return
        if not messagebox.askyesno(
            "対応を確認しましたか？",
            f"一覧のSpotify楽曲とYouTube動画の対応を確認しましたか？\n\n"
            f"確認済みの {len(selected_tracks)} 曲を保存します。",
            parent=self,
        ):
            return
        if not ffmpeg_available(self.config_data.ffmpeg_path):
            messagebox.showerror(
                "FFmpegが必要です",
                "MP3変換に使うffmpeg.exeが見つかりません。\n\n"
                "FFmpegをインストールしてPATHへ追加するか、設定からffmpeg.exeを選択してください。",
                parent=self,
            )
            return
        self.cancel_event.clear()
        self._set_busy(True, cancellable=True)
        self.cancel_button.configure(text="保存を停止")
        playlist = self.playlist
        total = len(selected_tracks)

        def work() -> None:
            completed = skipped = failed = 0
            errors: list[str] = []

            def track_progress(ratio: float | None, label: str) -> None:
                overall = ((completed + (ratio or 0)) / max(total, 1)) * 100
                self.events.put(("progress", (overall, label)))

            downloader = AudioDownloader(self.config_data, self.cancel_event, track_progress)
            for index, track in selected_tracks:
                if self.cancel_event.is_set():
                    break
                self.events.put(("track_status", (index, "処理中")))
                try:
                    path, was_skipped = downloader.download_track(playlist, track)
                    if was_skipped:
                        skipped += 1
                        status = "既存をスキップ"
                    else:
                        status = "保存済み"
                    self.events.put(("track_status", (index, status)))
                except DownloadCancelled:
                    self.events.put(("track_status", (index, "キャンセル")))
                    break
                except Exception as exc:
                    failed += 1
                    detail = str(exc)
                    if len(detail) > 350:
                        detail = detail[:347] + "..."
                    errors.append(f"{track.artist_text} - {track.name}\n  {detail}")
                    self.events.put(("track_status", (index, "失敗")))
                    self.events.put(("log_error", f"{track.artist_text} - {track.name}\n{exc}"))
                completed += 1
                self.events.put(("progress", (completed / max(total, 1) * 100, f"{completed}/{total} 曲")))
            cancelled = self.cancel_event.is_set()
            self.events.put(("download_done", (completed, skipped, failed, cancelled, errors)))
            self.events.put(("idle", None))

        self._run_worker(work)

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "idle":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self._set_busy(False)
                elif kind == "auth_required":
                    self.progress_label.configure(text="ブラウザでSpotify認証中")
                elif kind == "auth_changed":
                    self._update_connection_label()
                elif kind == "playlist":
                    self._show_playlist(payload)
                elif kind == "progress":
                    percent, label = payload
                    self.progress.configure(value=percent)
                    self.progress_label.configure(text=label)
                elif kind == "track_status":
                    index, status = payload
                    self._set_track_status(index, status)
                elif kind == "mapping":
                    index, thumbnail, status = payload
                    self._show_mapping(index, thumbnail, status)
                elif kind == "mapping_error":
                    index, detail = payload
                    self._show_mapping_error(index, detail)
                elif kind == "mapping_done":
                    self._mapping_finished(*payload)
                elif kind == "error":
                    title, message = payload
                    messagebox.showerror(title, message, parent=self)
                    self.progress_label.configure(text="エラー")
                elif kind == "log_error":
                    self._append_error(payload)
                elif kind == "download_done":
                    self._download_finished(*payload)
                elif kind == "update_result":
                    release, silent = payload
                    self._handle_update_result(release, silent)
                elif kind == "update_check_error":
                    message, silent = payload
                    self._finish_update_check()
                    if not silent:
                        messagebox.showerror("更新確認に失敗しました", message, parent=self)
                elif kind == "update_progress":
                    self.progress.configure(value=payload * 100)
                    self.progress_label.configure(text=f"更新をダウンロード中 {payload * 100:.0f}%")
                elif kind == "update_ready":
                    self._apply_downloaded_update(payload)
                elif kind == "update_download_error":
                    self._set_busy(False)
                    messagebox.showerror("更新に失敗しました", payload, parent=self)
                elif kind == "update_cancelled":
                    self._set_busy(False)
                    self.progress_label.configure(text="更新をキャンセルしました")
        except queue.Empty:
            pass
        self.after(80, self._drain_events)

    def _show_playlist(self, playlist: Playlist) -> None:
        self.playlist = playlist
        self.mapping_ready = False
        self.thumbnail_images.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        placeholder = self._placeholder_image()
        for index, track in enumerate(playlist.tracks):
            self.tree.insert(
                "", "end", iid=str(index), image=placeholder,
                values=(
                    track.position, track.name, track.artist_text, track.duration_text,
                    "未検索", "--", track.status,
                ),
            )
        self.playlist_label.configure(text=playlist.name)
        owner = f"・{playlist.owner}" if playlist.owner else ""
        self.count_label.configure(text=f"{len(playlist.tracks)} 曲 {owner}")
        self.progress.configure(value=0)
        self.progress_label.configure(text="次に対応を検索してください")
        self._update_action_states()

    def _show_mapping(self, index: int, thumbnail: bytes, status: str) -> None:
        if not self.playlist or index >= len(self.playlist.tracks):
            return
        track = self.playlist.tracks[index]
        track.status = status
        if thumbnail:
            try:
                source = Image.open(io.BytesIO(thumbnail)).convert("RGB")
                source.thumbnail((120, 68), Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", (120, 68), PANEL)
                canvas.paste(source, ((120 - source.width) // 2, (68 - source.height) // 2))
                self.thumbnail_images[str(index)] = ImageTk.PhotoImage(canvas)
            except (OSError, ValueError):
                self.thumbnail_images[str(index)] = self._placeholder_image()
        else:
            self.thumbnail_images[str(index)] = self._placeholder_image()
        self._refresh_row(index)
        self.tree.see(str(index))

    def _show_mapping_error(self, index: int, detail: str) -> None:
        if not self.playlist or index >= len(self.playlist.tracks):
            return
        track = self.playlist.tracks[index]
        track.status = "候補なし・除外"
        track.excluded = True
        self._refresh_row(index)
        print(f"{track.artist_text} - {track.name}\n{detail}", file=sys.stderr)

    def _mapping_finished(self, matched: int, failed: int, cancelled: bool) -> None:
        self.mapping_ready = not cancelled and matched > 0
        if cancelled:
            self.progress_label.configure(text="対応検索を停止しました")
            messagebox.showinfo(
                "検索を停止しました",
                "ダウンロードは開始していません。対応を検索し直すことができます。",
                parent=self,
            )
        else:
            self.progress.configure(value=100)
            self.progress_label.configure(text="対応一覧を確認してください")
            messagebox.showinfo(
                "対応検索が完了しました",
                f"候補あり: {matched} 曲\n候補なし: {failed} 曲\n\n"
                "サムネイルと曲名を確認し、誤りがあればその曲を除外してください。",
                parent=self,
            )
        self._update_action_states()

    def _refresh_row(self, index: int) -> None:
        if not self.playlist or index >= len(self.playlist.tracks) or not self.tree.exists(str(index)):
            return
        track = self.playlist.tracks[index]
        youtube_title = track.selected_video_title or "未検索"
        youtube_info = (
            f"{track.selected_video_uploader} ・ {track.youtube_duration_text}"
            if track.selected_video_url else "--"
        )
        status = "除外" if track.excluded and track.selected_video_url else track.status
        image = self.thumbnail_images.get(str(index), self._placeholder_image())
        self.tree.item(
            str(index), image=image,
            values=(
                track.position, track.name, track.artist_text, track.duration_text,
                youtube_title, youtube_info, status,
            ),
        )

    def _placeholder_image(self) -> tk.PhotoImage:
        image = self.thumbnail_images.get("__placeholder__")
        if isinstance(image, tk.PhotoImage):
            return image
        placeholder = tk.PhotoImage(width=120, height=68)
        placeholder.put(PANEL_2, to=(0, 0, 120, 68))
        self.thumbnail_images["__placeholder__"] = placeholder
        return placeholder

    def _set_track_status(self, index: int, status: str) -> None:
        if not self.playlist or index >= len(self.playlist.tracks):
            return
        track = self.playlist.tracks[index]
        track.status = status
        if self.tree.exists(str(index)):
            values = list(self.tree.item(str(index), "values"))
            values[-1] = status
            self.tree.item(str(index), values=values)
            self.tree.see(str(index))

    def _toggle_excluded(self) -> None:
        selection = self.tree.selection()
        if not selection or not self.playlist:
            return
        index = int(selection[0])
        track = self.playlist.tracks[index]
        if not track.selected_video_url:
            messagebox.showinfo("候補がありません", "YouTube候補がある曲だけ切り替えられます。", parent=self)
            return
        track.excluded = not track.excluded
        self._refresh_row(index)
        self._update_action_states()

    def _open_row_link(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row or not self.playlist:
            return
        track = self.playlist.tracks[int(row)]
        url = ""
        if column in {"#2", "#3", "#4"}:
            url = track.spotify_url
        elif column in {"#0", "#5", "#6"}:
            url = track.selected_video_url
        if url:
            webbrowser.open(url)

    def _tree_motion(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        clickable = bool(row and column in {"#0", "#2", "#3", "#4", "#5", "#6"})
        self.tree.configure(cursor="hand2" if clickable else "")

    def _download_finished(
        self, completed: int, skipped: int, failed: int, cancelled: bool, errors: list[str]
    ) -> None:
        if cancelled:
            self.progress_label.configure(text="キャンセルしました")
            messagebox.showinfo("キャンセル", f"{completed} 曲まで処理しました。", parent=self)
        else:
            self.progress.configure(value=100)
            self.progress_label.configure(text="完了")
            summary = f"処理: {completed} 曲\n既存スキップ: {skipped} 曲\n失敗: {failed} 曲"
            if errors:
                preview = "\n\n".join(errors[:5])
                if len(errors) > 5:
                    preview += f"\n\nほか {len(errors) - 5} 曲"
                messagebox.showwarning("一部の曲を保存できませんでした", f"{summary}\n\n{preview}", parent=self)
            else:
                messagebox.showinfo("保存完了", summary, parent=self)

    def _append_error(self, text: str) -> None:
        # Individual failures remain visible in the table; details are shown without halting the queue.
        self.progress_label.configure(text="一部の曲でエラー")
        print(text, file=sys.stderr)

    def _set_busy(self, busy: bool, cancellable: bool = False) -> None:
        self.busy = busy
        self.cancel_button.configure(state="normal" if busy and cancellable else "disabled")
        if not busy:
            self.cancel_button.configure(text="キャンセル")
        self._update_action_states()

    def _update_action_states(self) -> None:
        has_playlist = bool(self.playlist and self.playlist.tracks)
        has_selection = bool(self.tree.selection())
        has_downloads = bool(
            self.playlist
            and any(track.selected_video_url and not track.excluded for track in self.playlist.tracks)
        )
        self.load_button.configure(state="disabled" if self.busy else "normal")
        self.match_button.configure(state="normal" if has_playlist and not self.busy else "disabled")
        self.download_button.configure(
            state="normal" if self.mapping_ready and has_downloads and not self.busy else "disabled"
        )
        self.exclude_button.configure(
            state="normal" if self.mapping_ready and has_selection and not self.busy else "disabled"
        )
        if has_selection and self.playlist:
            track = self.playlist.tracks[int(self.tree.selection()[0])]
            self.exclude_button.configure(text="選択曲を戻す" if track.excluded else "選択曲を除外")
        else:
            self.exclude_button.configure(text="選択曲を除外")

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.progress_label.configure(text="キャンセル中…")

    def _check_for_updates(self, silent: bool = False) -> None:
        if self.update_checking:
            return
        if self.busy:
            if not silent:
                messagebox.showinfo("処理中です", "現在の処理が終わってから更新を確認してください。", parent=self)
            return
        self.update_checking = True
        self.update_button.configure(state="disabled", text="更新を確認中…")

        def work() -> None:
            try:
                release = GitHubUpdater(__version__).check()
                self.events.put(("update_result", (release, silent)))
            except Exception as exc:
                self.events.put(("update_check_error", (str(exc), silent)))

        self._run_worker(work)

    def _finish_update_check(self) -> None:
        self.update_checking = False
        self.update_button.configure(state="normal", text=f"v{__version__}・更新確認")

    def _handle_update_result(self, release: ReleaseInfo | None, silent: bool) -> None:
        self._finish_update_check()
        if not release:
            if not silent:
                messagebox.showinfo("更新確認", f"v{__version__} が最新版です。", parent=self)
            return
        notes = release.notes.strip()
        if len(notes) > 700:
            notes = notes[:697] + "..."
        detail = f"新しいバージョン v{release.version} があります。\n\n{notes}" if notes else (
            f"新しいバージョン v{release.version} があります。"
        )
        if not messagebox.askyesno("アップデート", f"{detail}\n\n今すぐ更新しますか？", parent=self):
            return
        if not getattr(sys, "frozen", False):
            messagebox.showinfo(
                "開発版で実行中です",
                "自動適用はパッケージ版で利用できます。リリースページを開きます。",
                parent=self,
            )
            if release.page_url:
                webbrowser.open(release.page_url)
            return
        self._download_update(release)

    def _download_update(self, release: ReleaseInfo) -> None:
        self.cancel_event.clear()
        self._set_busy(True, cancellable=True)
        self.cancel_button.configure(text="更新を停止")
        self.progress.configure(mode="determinate", value=0)
        self.progress_label.configure(text=f"v{release.version} をダウンロード中")

        def work() -> None:
            try:
                updater = GitHubUpdater(__version__)
                staging = updater.download(
                    release,
                    lambda ratio: self.events.put(("update_progress", ratio)),
                    self.cancel_event,
                )
                self.events.put(("update_ready", staging))
            except UpdateCancelled:
                self.events.put(("update_cancelled", None))
            except Exception as exc:
                self.events.put(("update_download_error", str(exc)))

        self._run_worker(work)

    def _apply_downloaded_update(self, staging: Path) -> None:
        try:
            launch_update_and_restart(staging)
        except Exception as exc:
            self._set_busy(False)
            messagebox.showerror("更新を開始できません", str(exc), parent=self)
            return
        self.destroy()

    def _open_output(self) -> None:
        folder = self.config_data.resolved_output_dir
        folder.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["explorer", str(folder)])
        except OSError as exc:
            messagebox.showerror("フォルダーを開けません", str(exc), parent=self)

    def _update_connection_label(self) -> None:
        text = "Spotify: 接続済み" if self.spotify.is_connected else "Spotify: 未接続"
        self.connection_label.configure(text=text)

    def _open_settings(self) -> None:
        if self.busy:
            return
        dialog = SettingsDialog(self, self.config_data, self.spotify.is_connected)
        self.wait_window(dialog)
        if not dialog.result:
            return
        old_client_id = self.config_data.client_id
        old_spotify = self.spotify
        self.config_data = dialog.result
        self.config_store.save(self.config_data)
        if old_client_id != self.config_data.client_id or self.spotify.redirect_port != self.config_data.redirect_port:
            self.spotify = SpotifyClient(self.config_data.client_id, self.config_data.redirect_port)
        if dialog.disconnect_requested:
            old_spotify.disconnect()
            if self.spotify is not old_spotify:
                self.spotify.disconnect()
        self._update_connection_label()

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno("終了", "処理中です。キャンセルして終了しますか？", parent=self):
            return
        self.cancel_event.set()
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: PlaylistAudioSaverApp, config: AppConfig, connected: bool) -> None:
        super().__init__(parent)
        self.title("設定")
        self.geometry("650x540")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()
        self.result: AppConfig | None = None
        self.disconnect_requested = False
        self.client_id = tk.StringVar(value=config.client_id)
        self.output_dir = tk.StringVar(value=str(config.resolved_output_dir))
        self.market = tk.StringVar(value=config.market)
        self.ffmpeg_path = tk.StringVar(value=config.ffmpeg_path)
        self.audio_quality = tk.StringVar(value=config.audio_quality)
        self.search_results = tk.IntVar(value=config.search_results)
        self.skip_existing = tk.BooleanVar(value=config.skip_existing)
        self.check_updates = tk.BooleanVar(value=config.check_updates)
        self.redirect_port = tk.IntVar(value=config.redirect_port)
        self._build(connected)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(20, lambda: self.focus_force())

    def _build(self, connected: bool) -> None:
        body = ttk.Frame(self, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Spotify連携", font=("Segoe UI Semibold", 14)).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text="Client ID", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(15, 5))
        ttk.Entry(body, textvariable=self.client_id, width=54).grid(row=2, column=0, columnspan=3, sticky="ew")
        redirect = f"http://127.0.0.1:{self.redirect_port.get()}/callback"
        ttk.Label(body, text=f"Spotify DashboardのRedirect URI: {redirect}", style="Muted.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))

        ttk.Label(body, text="保存", font=("Segoe UI Semibold", 14)).grid(row=4, column=0, columnspan=3, sticky="w", pady=(22, 0))
        ttk.Label(body, text="保存先", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(12, 5))
        ttk.Entry(body, textvariable=self.output_dir).grid(row=6, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        ttk.Button(body, text="参照", command=self._browse_output).grid(row=6, column=2)
        ttk.Label(body, text="FFmpeg（空欄ならPATHから検索）", style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=(12, 5))
        ttk.Entry(body, textvariable=self.ffmpeg_path).grid(row=8, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        ttk.Button(body, text="参照", command=self._browse_ffmpeg).grid(row=8, column=2)

        options = ttk.Frame(body, style="Panel.TFrame", padding=14)
        options.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(18, 0))
        ttk.Label(options, text="市場", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.market, width=7).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(options, text="音質 (0=最高)", style="Panel.TLabel").grid(row=0, column=1, sticky="w", padx=(30, 0))
        ttk.Spinbox(options, from_=0, to=9, textvariable=self.audio_quality, width=7).grid(row=1, column=1, sticky="w", padx=(30, 0), pady=(4, 0))
        ttk.Label(options, text="検索候補数", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(30, 0))
        ttk.Spinbox(options, from_=1, to=10, textvariable=self.search_results, width=7).grid(row=1, column=2, sticky="w", padx=(30, 0), pady=(4, 0))
        ttk.Checkbutton(options, text="同名ファイルはスキップ", variable=self.skip_existing).grid(row=2, column=0, columnspan=3, sticky="w", pady=(14, 0))
        ttk.Checkbutton(options, text="起動時にGitHubで更新を確認", variable=self.check_updates).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(22, 0))
        if connected:
            ttk.Button(buttons, text="Spotify接続を解除", command=self._disconnect).pack(side="left")
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="保存", style="Accent.TButton", command=self._save).pack(side="right")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

    def _browse_output(self) -> None:
        value = filedialog.askdirectory(parent=self, initialdir=self.output_dir.get())
        if value:
            self.output_dir.set(value)

    def _browse_ffmpeg(self) -> None:
        value = filedialog.askopenfilename(parent=self, title="ffmpeg.exeを選択", filetypes=[("ffmpeg", "ffmpeg.exe"), ("実行ファイル", "*.exe")])
        if value:
            self.ffmpeg_path.set(value)

    def _disconnect(self) -> None:
        self.disconnect_requested = True
        messagebox.showinfo("Spotify", "設定を保存すると接続情報を削除します。", parent=self)

    def _save(self) -> None:
        client_id = self.client_id.get().strip()
        if client_id and (len(client_id) < 16 or not client_id.isalnum()):
            messagebox.showerror("Client ID", "Spotify Client IDの形式を確認してください。", parent=self)
            return
        market = self.market.get().strip().upper()
        if len(market) != 2 or not market.isalpha():
            messagebox.showerror("市場", "JPのような2文字の国コードを入力してください。", parent=self)
            return
        try:
            quality = str(max(0, min(int(self.audio_quality.get()), 9)))
            results = max(1, min(int(self.search_results.get()), 10))
        except (ValueError, tk.TclError):
            messagebox.showerror("設定", "音質と検索候補数は数値で入力してください。", parent=self)
            return
        self.result = AppConfig(
            client_id=client_id,
            output_dir=self.output_dir.get().strip(),
            market=market,
            ffmpeg_path=self.ffmpeg_path.get().strip(),
            audio_quality=quality,
            search_results=results,
            skip_existing=bool(self.skip_existing.get()),
            check_updates=bool(self.check_updates.get()),
            redirect_port=int(self.redirect_port.get()),
        )
        self.destroy()


def _candidate_status(track: Track, candidate: SearchCandidate) -> str:
    expected = track.duration_ms / 1000
    duration_difference = abs(candidate.duration - expected) if candidate.duration and expected else 0
    allowed_difference = max(15, expected * 0.08)
    if candidate.score < 0.85 or duration_difference > allowed_difference:
        return "要確認"
    return "対応候補"


def _enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def main() -> None:
    _enable_windows_dpi_awareness()
    app = PlaylistAudioSaverApp()
    app.mainloop()


if __name__ == "__main__":
    main()
