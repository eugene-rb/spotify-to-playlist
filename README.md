# Playlist Audio Saver

[![Test](https://github.com/eugene-rb/spotify-to-playlist/actions/workflows/ci.yml/badge.svg)](https://github.com/eugene-rb/spotify-to-playlist/actions/workflows/ci.yml)

Spotifyプレイリストの曲情報を読み取り、各曲をYouTubeで検索し、`yt-dlp`で音声を取得してタグ付きMP3として保存するWindows 11向けデスクトップアプリです。

## できること

- SpotifyプレイリストURL / URIの読み込み
- タイトル、アーティスト、長さを比較したYouTube候補の自動選択
- Spotify楽曲とYouTube動画の対応を、動画サムネイル・投稿者・双方の再生時間付きで事前確認
- Spotify曲名またはYouTube動画欄のダブルクリックで元ページを表示
- 誤対応の曲を個別に除外し、検索中・保存中はいつでもキャンセル可能
- `yt-dlp` + FFmpegによるMP3変換
- Spotify由来のタイトル、アーティスト、アルバム、発売日、トラック番号、ディスク番号、ISRC、未加工カバー画像のID3埋め込み
- Spotify曲URL、YouTube取得元URL、Spotify帰属情報のタグ保存
- 既存ファイルのスキップ、途中キャンセル、曲単位のエラー継続
- SpotifyトークンをWindows資格情報マネージャーへ保存
- GitHub Releasesを利用した起動時の自動更新確認とワンクリック更新

## ダウンロード

[最新のWindows版インストーラーをダウンロード](https://github.com/eugene-rb/spotify-to-playlist/releases/latest/download/PlaylistAudioSaver-Setup.exe)

`PlaylistAudioSaver-Setup.exe`を実行してインストールしてください。管理者権限は不要です。FFmpegは別途必要です。

## 必要なもの

- Windows 11
- Python 3.11以降
- [FFmpeg](https://ffmpeg.org/download.html)（`ffmpeg.exe`をPATHへ追加するか、アプリの設定で選択）
- Spotify PremiumアカウントとSpotify Developer App

Spotifyの現行Web APIでは、プレイリスト項目は認証ユーザーが所有または共同編集するプレイリストに制限されています。第三者所有の公開プレイリストは403になる場合があります。

## Spotify Developer Appの準備

1. [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) でアプリを作成し、Web APIを有効にします。
2. Redirect URIへ次を正確に登録します。

   ```text
   http://127.0.0.1:43821/callback
   ```

3. アプリの「設定」を開き、「Dashboardを開く」「コピー」「貼り付け」の順に操作してClient IDを設定します。Client Secretは不要です。
4. 初回読み込み時にブラウザが開くのでSpotifyへログインして許可します。

## 基本操作

1. SpotifyプレイリストURLを入力して「読み込む」を押します。
2. 「対応を検索」を押します。この段階では音声をダウンロードしません。
3. Spotify楽曲とYouTube候補の一覧を確認します。サムネイル、曲名、投稿者、時間を比較でき、リンク部分をダブルクリックするとブラウザで元ページを開けます。
4. 誤対応があれば行を選択して「選択曲を除外」を押します。検索自体を止める場合は「キャンセル」を押します。
5. 問題がなければ「確認済みを保存」を押します。確認ダイアログの後、除外されていない曲だけを保存します。

## 開発版を起動

PowerShellで次を実行します。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\scripts\run.ps1
```

または仮想環境を有効化して直接起動できます。

```powershell
.\.venv\Scripts\python.exe -m playlist_audio_saver
```

## Windowsインストーラーをビルド

[Inno Setup 6](https://jrsoftware.org/isinfo.php)をインストールしてから実行します。

```powershell
.\scripts\build.ps1
```

単一EXEインストーラーの生成先は `dist\installer\PlaylistAudioSaver-Setup.exe` です。アプリ本体は `dist\PlaylistAudioSaver` にも生成されます。FFmpegは同梱しないため、利用するPCにも別途FFmpegが必要です。

## 自動アップデート

パッケージ版は起動時にGitHub Releasesの最新版を確認します。更新がある場合は確認画面を表示し、同意後にWindows版ZIPをダウンロードします。Releaseに同梱した更新マニフェストのSHA-256ダイジェストを検証し、一致した場合だけアプリ終了後にファイルを差し替えて再起動します。

設定から起動時の確認を無効にでき、画面上部の「更新確認」から手動確認もできます。ソースから直接起動している開発版では自動差し替えを行いません。

新しいリリースは、`pyproject.toml`と`src/playlist_audio_saver/__init__.py`のバージョンを更新し、同じ番号のタグをpushするとGitHub Actionsが自動でテスト、Windowsビルド、Release公開を行います。

## テスト

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## 保存先とファイル名

既定では `ミュージック\Playlist Audio Saver\<プレイリスト名>` に、次の形式で保存します。

```text
01 - アーティスト - 曲名.mp3
```

設定は `%APPDATA%\PlaylistAudioSaver\config.json`、Spotifyの認証トークンはWindows資格情報マネージャーに保存されます。

## 利用上の注意

このアプリはSpotifyまたはYouTubeの公式製品ではありません。Spotifyから音声を取得するものではなく、Spotify APIはメタデータ取得にのみ使用します。YouTubeの利用規約、著作権、各地域の法令に従い、自分が権利を持つ音源、明示的にダウンロードが許可された音源、またはパブリックドメイン等のコンテンツにのみ使用してください。
