from __future__ import annotations

import base64
import hashlib
import json
import re
from html.parser import HTMLParser
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import keyring
import requests

from .models import Playlist, Track


ACCOUNTS_URL = "https://accounts.spotify.com"
API_URL = "https://api.spotify.com/v1"
KEYRING_SERVICE = "PlaylistAudioSaver.Spotify"
SCOPES = "playlist-read-private playlist-read-collaborative"


class SpotifyError(RuntimeError):
    pass


def spotify_resource(value: str) -> tuple[str, str]:
    value = value.strip()
    if value.startswith("spotify:"):
        parts = value.split(":")
        if len(parts) != 3:
            raise ValueError("Spotify URIの形式を確認してください。")
        _, kind, resource_id = parts
    else:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {"open.spotify.com", "www.open.spotify.com"}:
            raise ValueError("Spotifyの共有リンクを入力してください。")
        parts = [part for part in parsed.path.split("/") if part]
        if parts and (parts[0].startswith("intl-") or parts[0] == "embed"):
            parts = parts[1:]
        if len(parts) != 2:
            raise ValueError("Spotifyリンクの形式を確認してください。")
        kind, resource_id = parts
    if kind not in {"playlist", "album", "track"} or not re.fullmatch(r"[A-Za-z0-9]+", resource_id):
        raise ValueError("プレイリスト・アルバム・シングル・曲のリンクを入力してください。")
    return kind, resource_id


def playlist_id_from_url(value: str) -> str:
    value = value.strip()
    if value.startswith("spotify:playlist:"):
        playlist_id = value.rsplit(":", 1)[-1]
    else:
        parsed = urlparse(value)
        if parsed.netloc.lower() not in {"open.spotify.com", "www.open.spotify.com"}:
            raise ValueError("SpotifyプレイリストのURLを入力してください。")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[-2] != "playlist":
            raise ValueError("SpotifyプレイリストのURLを入力してください。")
        playlist_id = parts[-1]
    if not playlist_id or not playlist_id.replace("-", "").isalnum():
        raise ValueError("プレイリストIDを読み取れませんでした。")
    return playlist_id


class _OAuthResult:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.code = ""
        self.state = ""
        self.error = ""


def _callback_handler(result: _OAuthResult) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            result.code = query.get("code", [""])[0]
            result.state = query.get("state", [""])[0]
            result.error = query.get("error", [""])[0]
            result.event.set()
            ok = bool(result.code) and not result.error
            title = "Spotify連携が完了しました" if ok else "Spotify連携に失敗しました"
            body = (
                "このウィンドウを閉じてアプリに戻ってください。"
                if ok
                else "認証がキャンセルされたか、エラーが発生しました。"
            )
            html = (
                "<!doctype html><meta charset='utf-8'><title>Playlist Audio Saver</title>"
                "<style>body{font-family:Segoe UI,sans-serif;background:#111827;color:#f9fafb;"
                "display:grid;place-items:center;height:90vh}main{max-width:540px;padding:36px;"
                "background:#1f2937;border-radius:16px}h1{font-size:24px}</style>"
                f"<main><h1>{title}</h1><p>{body}</p></main>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


class SpotifyClient:
    def __init__(self, client_id: str, redirect_port: int = 43821) -> None:
        self.client_id = client_id.strip()
        self.redirect_port = redirect_port
        self.session = requests.Session()
        self._token: dict[str, Any] | None = self._load_token()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.redirect_port}/callback"

    @property
    def is_connected(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def _load_token(self) -> dict[str, Any] | None:
        if not self.client_id:
            return None
        try:
            value = keyring.get_password(KEYRING_SERVICE, self.client_id)
            return json.loads(value) if value else None
        except (keyring.errors.KeyringError, ValueError, TypeError):
            return None

    def _save_token(self, token: dict[str, Any]) -> None:
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 30
        if self._token and self._token.get("refresh_token") and not token.get("refresh_token"):
            token["refresh_token"] = self._token["refresh_token"]
        self._token = token
        try:
            keyring.set_password(KEYRING_SERVICE, self.client_id, json.dumps(token))
        except keyring.errors.KeyringError as exc:
            raise SpotifyError(f"Windows資格情報マネージャーへ保存できません: {exc}") from exc

    def disconnect(self) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, self.client_id)
        except keyring.errors.KeyringError:
            pass
        self._token = None

    def authorize(self, timeout: int = 180) -> None:
        if not self.client_id:
            raise SpotifyError("設定でSpotify Client IDを入力してください。")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(24)
        result = _OAuthResult()
        try:
            server = HTTPServer(("127.0.0.1", self.redirect_port), _callback_handler(result))
        except OSError as exc:
            raise SpotifyError(f"認証用ポート {self.redirect_port} を開けません: {exc}") from exc
        server.timeout = 1
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }
        webbrowser.open(f"{ACCOUNTS_URL}/authorize?{urlencode(params)}")
        deadline = time.monotonic() + timeout
        while not result.event.is_set() and time.monotonic() < deadline:
            server.handle_request()
        server.server_close()
        if not result.event.is_set():
            raise SpotifyError("Spotify認証がタイムアウトしました。")
        if result.error:
            raise SpotifyError(f"Spotify認証が拒否されました: {result.error}")
        if result.state != state:
            raise SpotifyError("認証応答のstateが一致しません。")
        response = self.session.post(
            f"{ACCOUNTS_URL}/api/token",
            data={
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "code": result.code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            },
            timeout=30,
        )
        if not response.ok:
            raise SpotifyError(_spotify_error(response, "トークンを取得できませんでした"))
        self._save_token(response.json())

    def _access_token(self) -> str:
        if not self._token:
            raise SpotifyError("Spotifyへ接続してください。")
        if float(self._token.get("expires_at", 0)) <= time.time():
            refresh_token = self._token.get("refresh_token")
            if not refresh_token:
                raise SpotifyError("Spotifyへ再接続してください。")
            response = self.session.post(
                f"{ACCOUNTS_URL}/api/token",
                data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=30,
            )
            if not response.ok:
                raise SpotifyError(_spotify_error(response, "認証を更新できませんでした"))
            self._save_token(response.json())
        return str(self._token["access_token"])

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(4):
            response = self.session.get(
                f"{API_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._access_token()}"},
                timeout=30,
            )
            if response.status_code == 429 and attempt < 3:
                wait = min(int(response.headers.get("Retry-After", "1")), 10)
                time.sleep(max(wait, 1))
                continue
            if response.status_code == 401 and attempt == 0 and self._token:
                self._token["expires_at"] = 0
                continue
            if not response.ok:
                raise SpotifyError(_spotify_error(response, "Spotify APIエラー"))
            return response.json()
        raise SpotifyError("Spotify APIのレート制限が続いています。しばらく待ってください。")

    def get_playlist(self, value: str, market: str = "JP") -> Playlist:
        playlist_id = playlist_id_from_url(value)
        info = self._get(f"/playlists/{playlist_id}", {"market": market})
        playlist = Playlist(
            spotify_id=playlist_id,
            name=info.get("name") or "Spotify Playlist",
            owner=(info.get("owner") or {}).get("display_name") or "",
            spotify_url=(info.get("external_urls") or {}).get("spotify") or value,
            cover_url=_first_image(info.get("images")),
        )
        offset = 0
        while True:
            page = self._get(
                f"/playlists/{playlist_id}/items",
                {"market": market, "limit": 50, "offset": offset, "additional_types": "track"},
            )
            items = page.get("items") or []
            for item in items:
                track_data = item.get("track") or item.get("item") or {}
                track = _parse_track(track_data, len(playlist.tracks) + 1)
                if track:
                    playlist.tracks.append(track)
            offset += len(items)
            if not page.get("next") or not items:
                break
        return playlist

    def get_collection(self, value: str, market: str = "JP") -> Playlist:
        kind, resource_id = spotify_resource(value)
        if self.is_connected:
            try:
                return self._get_api_collection(kind, resource_id, market)
            except SpotifyError:
                # Public embeds remain readable for third-party playlists whose
                # item endpoint is unavailable to Development Mode applications.
                pass
        try:
            return self.get_public_collection(kind, resource_id)
        except (SpotifyError, requests.RequestException) as exc:
            if self.is_connected:
                raise SpotifyError(f"Spotifyから取得できませんでした。公開状態や地域制限を確認してください。\n{exc}") from exc
            raise SpotifyError("公開ページを取得できません。非公開の場合は設定でSpotifyに接続してください。") from exc

    def _get_api_collection(self, kind: str, resource_id: str, market: str) -> Playlist:
        url = f"https://open.spotify.com/{kind}/{resource_id}"
        if kind == "playlist":
            return self.get_playlist(url, market)
        info = self._get(f"/{kind}s/{resource_id}", {"market": market})
        album = info if kind == "album" else info.get("album") or {}
        collection = Playlist(resource_id, info.get("name") or "Spotify", ", ".join(a["name"] for a in info.get("artists", [])),
                              url, _first_image(album.get("images")), kind=album.get("album_type", kind) if kind == "album" else "track")
        if kind == "track":
            track = _parse_track(info, 1)
            if track:
                collection.tracks.append(track)
            return collection
        page = info.get("tracks") or self._get(f"/albums/{resource_id}/tracks", {"market": market, "limit": 50})
        offset = 0
        while True:
            items = page.get("items") or []
            for item in items:
                track = _parse_track({**item, "album": album}, len(collection.tracks) + 1)
                if track:
                    collection.tracks.append(track)
            offset += len(items)
            if not page.get("next") or not items:
                break
            page = self._get(f"/albums/{resource_id}/tracks", {"market": market, "limit": 50, "offset": offset})
        return collection

    def get_public_collection(self, kind: str, resource_id: str) -> Playlist:
        response = self.session.get(f"https://open.spotify.com/embed/{kind}/{resource_id}", timeout=30)
        response.raise_for_status()
        return parse_public_collection(response.text, kind, resource_id)


class _EmbedDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.active = dict(attrs).get("id") == "__NEXT_DATA__"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.parts.append(data)


def parse_public_collection(html: str, kind: str, resource_id: str) -> Playlist:
    parser = _EmbedDataParser()
    parser.feed(html)
    try:
        entity = json.loads("".join(parser.parts))["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SpotifyError("Spotifyの公開ページに曲情報がありません。") from exc
    if entity.get("id") != resource_id or entity.get("type") != kind:
        raise SpotifyError("指定されたSpotifyリンクの曲情報を確認できません。")
    images = (entity.get("coverArt") or {}).get("sources") or (entity.get("visualIdentity") or {}).get("image") or []
    artists = [a["name"] for a in entity.get("artists") or [] if a.get("name")]
    collection = Playlist(resource_id, entity.get("name") or entity.get("title") or "Spotify",
                          entity.get("subtitle") or ", ".join(artists), f"https://open.spotify.com/{kind}/{resource_id}",
                          _first_image(images), kind=kind,
                          source_note="公開ページ掲載分です。全曲や詳細なアルバム情報が含まれない場合があります。")
    entries = [entity] if kind == "track" else entity.get("trackList")
    if not isinstance(entries, list):
        raise SpotifyError("公開ページから曲一覧を取得できません。")
    for position, item in enumerate(entries, 1):
        uri = item.get("uri") or ""
        if not uri.startswith("spotify:track:") or not item.get("title"):
            continue
        names = [a["name"] for a in item.get("artists") or [] if a.get("name")]
        # Spotify separates artist names with comma + non-breaking space.
        names = names or [a.strip() for a in (item.get("subtitle") or "").split(",\u00a0") if a.strip()]
        if not names:
            continue
        release = entity.get("releaseDate") or {}
        collection.tracks.append(Track(
            position=position, spotify_id=uri.rsplit(":", 1)[-1], name=item["title"], artists=names,
            album=collection.name if kind == "album" else "", album_artists=[collection.owner] if kind == "album" else [],
            release_date=str(release.get("isoString", ""))[:10] if isinstance(release, dict) else "",
            track_number=position, disc_number=1, duration_ms=int(item.get("duration") or 0),
            explicit=bool(item.get("isExplicit")), isrc="", spotify_url=f"https://open.spotify.com/track/{uri.rsplit(':', 1)[-1]}",
            cover_url=collection.cover_url if kind in {"album", "track"} else ""))
    if entries and not collection.tracks:
        raise SpotifyError("この公開ページには対応する音楽トラックがありません。")
    return collection


def _first_image(images: Any) -> str:
    if not isinstance(images, list) or not images:
        return ""
    return str(images[0].get("url") or "")


def _parse_track(data: dict[str, Any], position: int) -> Track | None:
    if not data or data.get("type") not in (None, "track") or data.get("is_local"):
        return None
    album = data.get("album") or {}
    artists = [a.get("name", "") for a in data.get("artists") or [] if a.get("name")]
    if not data.get("name") or not artists:
        return None
    return Track(
        position=position,
        spotify_id=str(data.get("id") or ""),
        name=str(data["name"]),
        artists=artists,
        album=str(album.get("name") or ""),
        album_artists=[a.get("name", "") for a in album.get("artists") or [] if a.get("name")],
        release_date=str(album.get("release_date") or ""),
        track_number=int(data.get("track_number") or position),
        disc_number=int(data.get("disc_number") or 1),
        duration_ms=int(data.get("duration_ms") or 0),
        explicit=bool(data.get("explicit")),
        isrc=str((data.get("external_ids") or {}).get("isrc") or ""),
        spotify_url=str((data.get("external_urls") or {}).get("spotify") or ""),
        cover_url=_first_image(album.get("images")),
    )


def _spotify_error(response: requests.Response, prefix: str) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload)
        message = error.get("message") if isinstance(error, dict) else str(error)
    except ValueError:
        message = response.text[:200]
    hint = ""
    if response.status_code == 403:
        hint = "（現在のユーザーが所有または共同編集するプレイリストか確認してください）"
    return f"{prefix} ({response.status_code}): {message or '不明なエラー'}{hint}"
