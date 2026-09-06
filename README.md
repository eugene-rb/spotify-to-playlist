# Playlist Audio Saver

[![Test](https://github.com/eugene-rb/spotify-to-playlist/actions/workflows/ci.yml/badge.svg)](https://github.com/eugene-rb/spotify-to-playlist/actions/workflows/ci.yml)

Spotifyのプレイリスト・アルバム・シングル・曲を読み取り、YouTubeの対応候補を確認・訂正して、タグ付きMP3として保存するWindows 11向けデスクトップアプリです。GUIはWPF / .NET 10のFluentテーマを使用し、Windowsのライト・ダーク設定、標準タイトルバー、スナップ、高DPI表示に対応します。

## できること

- プレイリスト・アルバム・シングル（albumリンク）・単曲のSpotify URL / URIの自動判別
- 自分以外の公開プレイリストも、Spotifyの公開埋め込みページから読み込み可能（Client ID不要）
- 曲名の一致を最優先し、投稿者がアーティスト本人・`- Topic`・`VEVO`・主要レーベルかを加味してYouTube候補を自動選択。歌詞・カバー・カラオケ・ライブ・編集版は自動的に下げる
- 曲名がはっきり一致しない候補は「要確認」表示（アーティストが合っていても別の曲を自信ありに見せない）
- Spotify楽曲とYouTube動画の対応を、動画サムネイル・投稿者・双方の再生時間付きで事前確認
- 曲を選び「Spotify ↗」「YouTube ↗」で元ページを表示
- 検索は曲名・アーティストの組み合わせを複数試し、0件や候補が弱いときは自動でクエリを緩めて再検索
- 「候補を訂正」でYouTube候補を一覧表示。サムネイル付きで選ぶか、動画URLを直接指定して差し替え（動画情報を再取得して反映）
- 手動で選んだ動画は訂正履歴に保存し、次回の検索でその曲へ自動適用。同じアーティストの他の曲でも、選んだチャンネルを優先（設定から消去可能）
- 誤対応の曲を個別に除外し、検索中・保存中は停止可能
- `yt-dlp` + FFmpegによるMP3変換
- 取得できたSpotify由来のタイトル、アーティスト、アルバム、発売日、トラック番号、ディスク番号、ISRC、カバー画像をID3に埋め込み
- Spotify曲URL、YouTube取得元URL、Spotify帰属情報のタグ保存
- 既存ファイルのスキップ、途中キャンセル、曲単位のエラー継続
- SpotifyトークンをWindows資格情報マネージャーへ保存
- GitHub Releasesを利用した起動時の自動更新確認とワンクリック更新

## ダウンロード

[最新のWindows版インストーラーをダウンロード](https://github.com/eugene-rb/spotify-to-playlist/releases/latest/download/PlaylistAudioSaver-Setup.exe)

`PlaylistAudioSaver-Setup.exe`を実行してインストールしてください。管理者権限は不要です。FFmpegは別途必要です。

## 必要なもの

- Windows 11
- 開発時のみ: Python 3.11以降、[.NET 10 SDK](https://dotnet.microsoft.com/download/dotnet/10.0)。インストーラーには両方の実行環境を同梱
- [FFmpeg](https://ffmpeg.org/download.html)（`ffmpeg.exe`をPATHへ追加するか、アプリの設定で選択）
- 非公開プレイリストやAPI経由の詳細情報取得: Spotify Developer Appと、その利用条件を満たすSpotifyアカウント

公開リンクは接続設定なしで利用できます。Spotify接続済みの場合はWeb APIを優先し、第三者所有のプレイリストなどAPIで取得できない場合は公開ページを使用します。公開ページに掲載される曲数・メタデータには制限があり、全曲取得を保証するものではありません。公開ページを使用した場合は画面に明示します。非公開・削除済み・地域制限のあるコンテンツは取得できない場合があります。公開ページの形式変更で取得できなくなる可能性もあります。

## Spotify Developer Appの準備

1. [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) でアプリを作成し、Web APIを有効にします。
2. Redirect URIへ次を正確に登録します。

   ```text
   http://127.0.0.1:43821/callback
   ```

3. アプリの「設定」を開き、Client IDを貼り付けて「設定を保存」を押します。Client Secretは不要です。
4. 「Spotifyに接続」でブラウザーが開くのでSpotifyへログインして許可します。非公開リンクの初回読み込み時にも認証できます。

## 基本操作

1. Spotifyの共有リンクまたはURIを入力して「読み込む」を押します。アルバム・シングル・単曲も同じ操作です。
2. 「対応を検索」を押します。この段階では音声をダウンロードしません。
3. サムネイル、曲名、投稿者、時間を比較します。行を選択して「Spotify ↗」「YouTube ↗」から元ページを確認できます。
4. 誤対応や「要確認」「候補なし」の曲は「候補を訂正」を押します。ダイアログでキーワードを変えてYouTubeを再検索し、候補一覧から選ぶか、動画URLを直接貼り付けます。保存しない曲は「除外」、検索・保存を中断する場合は「停止」を押します。
5. 「○ 曲を保存」を押します。確認ダイアログの後、除外されていない候補だけを保存します。手動訂正した曲は、同名の保存済みファイルも更新します。再検索しても、過去に手動訂正した曲は訂正履歴から自動で再適用されます（除外の設定はリセット）。訂正履歴は設定の「訂正履歴」から消去できます。

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

設定から起動時の確認を無効にでき、設定画面の「更新を確認」から手動確認もできます。ソースから直接起動している開発版では自動差し替えを行いません。

新しいリリースは、`pyproject.toml`と`src/playlist_audio_saver/__init__.py`のバージョンを更新し、同じ番号のタグをpushするとGitHub Actionsが自動でテスト、Windowsビルド、Release公開を行います。

## テスト

```powershell
.\.venv\Scripts\python.exe -m pytest
dotnet build native/PlaylistAudioSaver -c Release
```

Windowsのデスクトップとネット接続がある環境では、`powershell -ExecutionPolicy Bypass -File scripts/test-ui.ps1` で実GUIの公開曲読み込み・行選択・訂正・エラー復帰・設定ナビゲーションを確認できます。ユーザー設定は使わず、`build/ui-automation` にテスト設定を分離します。パッケージ版は `-AppPath dist/PlaylistAudioSaver/PlaylistAudioSaver.exe` で確認できます。

GUIは `native/PlaylistAudioSaver`、処理側は `src/playlist_audio_saver/backend.py` です。両者は標準入出力のJSONで通信し、認証情報はPython側だけで管理します。`--preview` はネットワークやユーザー設定への書き込みを行わない画面確認用です。`--populated`、`--settings`、`--compact`、`--dark` / `--light` と組み合わせられます。`--capture <PNGパス>` で画面を保存して終了します。

## 保存先とファイル名

既定では `ミュージック\Playlist Audio Saver\<プレイリスト名>` に、Spotifyの曲名をそのままファイル名にして保存します。

```text
曲名.mp3
```

同じプレイリスト内に同名の曲が複数ある場合は、2曲目以降に `曲名 (2).mp3` のように連番を付けます。アーティストやトラック番号などの情報はID3タグに埋め込まれます。

設定は `%APPDATA%\PlaylistAudioSaver\config.json`、訂正履歴は同じフォルダーの `corrections.json`、Spotifyの認証トークンはWindows資格情報マネージャーに保存されます。

## 利用上の注意

このアプリはSpotifyまたはYouTubeの公式製品ではありません。Spotifyから音声を取得するものではなく、Spotify APIはメタデータ取得にのみ使用します。YouTubeの利用規約、著作権、各地域の法令に従い、自分が権利を持つ音源、明示的にダウンロードが許可された音源、またはパブリックドメイン等のコンテンツにのみ使用してください。
