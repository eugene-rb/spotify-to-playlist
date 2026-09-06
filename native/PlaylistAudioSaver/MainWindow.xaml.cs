using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Win32;

namespace PlaylistAudioSaver;

public partial class MainWindow : Window
{
    public ObservableCollection<TrackRow> Tracks { get; } = [];
    private readonly BackendClient backend = new();
    private JsonObject? config;
    private bool ready, busy, mappingReady, packaged, connected, closing, initialState = true;
    private string outputDir = "", operation = "";
    private Action? afterIdle;
    private readonly bool preview = Environment.GetCommandLineArgs().Contains("--preview");

    public MainWindow()
    {
        InitializeComponent();
        DataContext = this;
        QualityInput.ItemsSource = Enumerable.Range(0, 10);
        ResultsInput.ItemsSource = Enumerable.Range(1, 10);
        backend.Message += message => Dispatcher.BeginInvoke(() => Handle(message));
        backend.Failed += message => Dispatcher.BeginInvoke(() => { ready = false; SetBusy(false); ShowError(message); });
        SetBusy(false);
    }

    private void Window_Loaded(object sender, RoutedEventArgs e)
    {
        if (preview) { LoadPreview(); return; }
        try { backend.Start(); }
        catch (Exception ex) { ShowError(ex.Message); }
    }

    private void Handle(JsonElement message)
    {
        if (closing) return;
        switch (message.Text("event"))
        {
            case "state":
                config = JsonNode.Parse(message.GetProperty("config").GetRawText())!.AsObject();
                outputDir = message.Text("output_dir");
                connected = message.Flag("connected"); packaged = message.Flag("packaged"); ready = true;
                ConnectionText.Text = connected ? "接続済み" : "公開リンクを利用できます";
                OutputText.Text = "保存先  ·  " + outputDir;
                VersionText.Text = "バージョン " + message.Text("version");
                AboutVersion.Text = "Playlist Audio Saver  ·  v" + message.Text("version");
                FfmpegStatus.Text = message.Flag("ffmpeg") ? "FFmpegを検出しました。MP3で保存できます。" : "FFmpeg未検出。保存するにはffmpeg.exeを指定してください。";
                FillSettings();
                if (initialState)
                {
                    initialState = false; StatusText.Text = "リンクを読み込んで、はじめましょう。";
                    if (config["check_updates"]!.GetValue<bool>())
                    {
                        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
                        timer.Tick += (_, _) => { timer.Stop(); if (!busy && ready) StartCommand(new { action = "check_update", silent = true }, "check_update", "更新を確認中…"); };
                        timer.Start();
                    }
                }
                UpdateActions();
                break;
            case "busy": SetBusy(true, message.Text("operation"), message.Flag("cancellable")); break;
            case "idle":
                if (operation == "check_update" && StatusText.Text == "更新を確認中…") StatusText.Text = "準備完了";
                mappingReady = message.Flag("mapping_ready"); SetBusy(false);
                var next = afterIdle; afterIdle = null; next?.Invoke();
                break;
            case "playlist":
                Tracks.Clear(); mappingReady = false;
                foreach (var data in message.GetProperty("tracks").EnumerateArray())
                {
                    var track = new TrackRow { Index = Tracks.Count }; track.Update(data); Tracks.Add(track);
                }
                CollectionTitle.Text = message.Text("name"); CollectionTitle.ToolTip = CollectionTitle.Text;
                var kind = message.Text("kind") switch { "album" => "アルバム", "single" => "シングル", "track" => "曲", "compilation" => "コンピレーション", _ => "プレイリスト" };
                CollectionInfo.Text = $"{kind}  ·  {Tracks.Count} 曲  ·  {message.Text("owner")}";
                SourceNote.Text = message.Text("source_note"); SourceNote.Visibility = SourceNote.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
                TrackGrid.Visibility = Tracks.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
                EmptyState.Visibility = Tracks.Count > 0 ? Visibility.Collapsed : Visibility.Visible;
                Progress.Value = 0; UpdateActions();
                break;
            case "track":
                var index = message.GetProperty("index").GetInt32();
                if (index >= 0 && index < Tracks.Count) Tracks[index].Update(message.GetProperty("track"), message);
                UpdateActions();
                break;
            case "progress":
                if (operation is not ("load" or "correct" or "check_update")) Progress.IsIndeterminate = false;
                Progress.Value = Math.Clamp(message.GetProperty("percent").GetDouble(), 0, 100);
                StatusText.Text = message.Text("message"); break;
            case "notice":
                StatusText.Text = message.Text("message");
                if (SettingsPage.Visibility == Visibility.Visible) SettingsStatus.Text = StatusText.Text;
                if (message.Text("detail") is { Length: > 0 } detail) ShowDialog("一部の曲を保存できませんでした", StatusText.Text + "\n\n" + detail);
                break;
            case "error":
                if (!message.Flag("silent")) ShowError(message.Text("message"));
                else StatusText.Text = "更新を確認できませんでした。設定から再確認できます。";
                break;
            case "configured": SettingsStatus.Text = "設定を保存しました。"; break;
            case "update":
                if (message.GetProperty("release").ValueKind != JsonValueKind.Null)
                {
                    var release = message.GetProperty("release").Clone();
                    afterIdle = () =>
                    {
                        if (!ShowDialog("アップデート", $"v{release.Text("version")} が利用できます。\n\n{release.Text("notes")[..Math.Min(700, release.Text("notes").Length)]}", packaged ? "更新する" : "リリースを開く")) return;
                        if (packaged) StartCommand(new { action = "download_update" }, "download_update", "更新をダウンロード中…");
                        else OpenLink(release.Text("page_url"));
                    };
                }
                else if (!message.Flag("silent")) { SettingsStatus.Text = "最新版を使用しています。"; StatusText.Text = "最新版を使用しています。"; }
                break;
            case "update_ready": afterIdle = () => Send(new { action = "apply_update" }); break;
            case "exit_for_update": busy = false; Close(); break;
        }
    }

    private void Send(object command)
    {
        try { backend.Send(command); }
        catch (Exception ex) { SetBusy(false); ShowError(ex.Message); }
    }

    private void StartCommand(object command, string action, string message)
    {
        if (busy || !ready) return;
        StatusText.Text = message;
        if (SettingsPage.Visibility == Visibility.Visible) SettingsStatus.Text = message;
        Progress.Value = 0;
        SetBusy(true, action, action is "match" or "download" or "download_update");
        Send(command);
    }

    private void SetBusy(bool value, string action = "", bool cancellable = false)
    {
        busy = value; operation = action;
        Progress.IsIndeterminate = value && action is "load" or "connect" or "correct" or "check_update";
        CancelButton.Visibility = value && cancellable ? Visibility.Visible : Visibility.Collapsed;
        CancelButton.IsEnabled = true;
        SettingsFields.IsEnabled = !value && ready;
        SettingsSaveButton.IsEnabled = !value && ready;
        UpdateActions();
    }

    private void UpdateActions()
    {
        if (!IsInitialized) return;
        var idle = ready && !busy;
        SettingsFields.IsEnabled = SettingsSaveButton.IsEnabled = idle;
        var selected = TrackGrid.SelectedItem as TrackRow;
        var count = Tracks.Count(t => t.VideoUrl.Length > 0 && !t.Excluded);
        LoadButton.IsEnabled = UrlInput.IsEnabled = idle;
        MatchButton.IsEnabled = idle && Tracks.Count > 0;
        SaveButton.IsEnabled = idle && mappingReady && count > 0;
        SaveButton.Content = count > 0 && mappingReady ? $"{count} 曲を保存" : "確認済みを保存";
        ExcludeButton.IsEnabled = idle && mappingReady && selected?.VideoUrl.Length > 0;
        ExcludeButton.Content = selected?.Excluded == true ? "戻す" : "除外";
        CorrectButton.IsEnabled = idle && selected != null;
        SpotifyLinkButton.IsEnabled = selected?.SpotifyUrl.Length > 0;
        YoutubeLinkButton.IsEnabled = selected?.VideoUrl.Length > 0;
        SelectionHint.Text = selected == null ? "曲を選んで候補を確認" : $"{selected.Position:00}  ·  {selected.Name}";
        UpdateButton.IsEnabled = idle;
    }

    private void Load_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(UrlInput.Text)) { UrlInput.Focus(); StatusText.Text = "Spotifyの共有リンクを入力してください。"; return; }
        StartCommand(new { action = "load", url = UrlInput.Text.Trim() }, "load", "Spotifyから読み込み中…");
    }
    private void Url_KeyDown(object sender, KeyEventArgs e) { if (e.Key == Key.Enter && LoadButton.IsEnabled) Load_Click(sender, e); }
    private void Match_Click(object sender, RoutedEventArgs e)
    {
        if (Tracks.Any(t => t.VideoUrl.Length > 0) && !ShowDialog("対応を検索し直す", "手動で指定した動画と除外の設定もリセットされます。", "検索し直す")) return;
        mappingReady = false;
        StartCommand(new { action = "match" }, "match", "YouTubeの候補を検索中…");
    }
    private void Save_Click(object sender, RoutedEventArgs e)
    {
        var count = Tracks.Count(t => t.VideoUrl.Length > 0 && !t.Excluded);
        if (ShowDialog("対応を確認して保存", $"Spotifyの曲とYouTubeの候補が一致していることを確認してください。\n\n{count} 曲をMP3で保存します。\n保存先: {outputDir}\n\n手動で訂正した曲は、同名の保存済みファイルも更新します。", "保存する"))
            StartCommand(new { action = "download" }, "download", "保存を開始しています…");
    }
    private void Cancel_Click(object sender, RoutedEventArgs e) { Send(new { action = "cancel" }); CancelButton.IsEnabled = false; StatusText.Text = "停止しています…"; }
    private void Track_SelectionChanged(object sender, SelectionChangedEventArgs e) => UpdateActions();
    private void Exclude_Click(object sender, RoutedEventArgs e) { if (TrackGrid.SelectedItem is TrackRow track) Send(new { action = "exclude", index = track.Index }); }
    private void SpotifyLink_Click(object sender, RoutedEventArgs e) { if (TrackGrid.SelectedItem is TrackRow track) OpenLink(track.SpotifyUrl); }
    private void YoutubeLink_Click(object sender, RoutedEventArgs e) { if (TrackGrid.SelectedItem is TrackRow track) OpenLink(track.VideoUrl); }
    private void Correct_Click(object sender, RoutedEventArgs e)
    {
        if (TrackGrid.SelectedItem is not TrackRow track) return;
        var input = new TextBox { Text = track.VideoUrl, Style = (Style)FindResource("Field"), MinWidth = 420, Margin = new Thickness(0, 16, 0, 0) };
        System.Windows.Automation.AutomationProperties.SetName(input, "正しいYouTube動画のURL");
        System.Windows.Automation.AutomationProperties.SetAutomationId(input, "CorrectionUrl");
        var search = new Button { Content = "YouTubeで曲を探す ↗", HorizontalAlignment = HorizontalAlignment.Left, Margin = new Thickness(0, 12, 0, 0) };
        search.Click += (_, _) => OpenLink("https://www.youtube.com/results?search_query=" + Uri.EscapeDataString(track.Name + " " + track.Subtitle.Split("  ·  ")[0]));
        var extra = new StackPanel(); extra.Children.Add(input); extra.Children.Add(search);
        if (ShowDialog("候補を訂正", $"{track.Name}\n{track.Subtitle}\n\n正しいYouTube動画のURLを指定してください。", "この動画に変更", extra))
            StartCommand(new { action = "correct", index = track.Index, url = input.Text.Trim() }, "correct", "指定した動画を確認中…");
    }
    private void Library_Click(object sender, RoutedEventArgs e) { if (LibraryPage is null) return; LibraryPage.Visibility = Visibility.Visible; SettingsPage.Visibility = Visibility.Collapsed; }
    private void Settings_Click(object sender, RoutedEventArgs e) { if (LibraryPage is null) return; LibraryPage.Visibility = Visibility.Collapsed; SettingsPage.Visibility = Visibility.Visible; }
    private void Dashboard_Click(object sender, RoutedEventArgs e) => OpenLink("https://developer.spotify.com/dashboard");
    private void CopyRedirect_Click(object sender, RoutedEventArgs e)
    {
        try { Clipboard.SetText(RedirectInput.Text); SettingsStatus.Text = "Redirect URIをコピーしました。"; }
        catch (System.Runtime.InteropServices.ExternalException) { SettingsStatus.Text = "コピーできませんでした。もう一度お試しください。"; }
    }
    private void FillSettings()
    {
        if (config is null) return;
        ClientIdInput.Text = config["client_id"]!.GetValue<string>();
        OutputInput.Text = outputDir; FfmpegInput.Text = config["ffmpeg_path"]!.GetValue<string>();
        MarketInput.Text = config["market"]!.GetValue<string>();
        QualityInput.SelectedItem = int.Parse(config["audio_quality"]!.GetValue<string>());
        ResultsInput.SelectedItem = config["search_results"]!.GetValue<int>();
        SkipCheck.IsChecked = config["skip_existing"]!.GetValue<bool>();
        UpdatesCheck.IsChecked = config["check_updates"]!.GetValue<bool>();
        RedirectInput.Text = $"http://127.0.0.1:{config["redirect_port"]}/callback";
        DisconnectButton.IsEnabled = connected;
        ConnectButton.IsEnabled = !connected && ClientIdInput.Text.Length > 0;
    }
    private void ResetSettings_Click(object sender, RoutedEventArgs e) { FillSettings(); SettingsStatus.Text = "保存済みの設定に戻しました。"; }
    private void SaveSettings_Click(object sender, RoutedEventArgs e)
    {
        if (busy || config is null) return;
        var updated = config.DeepClone().AsObject();
        updated["client_id"] = ClientIdInput.Text; updated["output_dir"] = OutputInput.Text;
        updated["ffmpeg_path"] = FfmpegInput.Text; updated["market"] = MarketInput.Text;
        updated["audio_quality"] = (QualityInput.SelectedItem ?? 0).ToString();
        updated["search_results"] = (int)(ResultsInput.SelectedItem ?? 5);
        updated["skip_existing"] = SkipCheck.IsChecked == true; updated["check_updates"] = UpdatesCheck.IsChecked == true;
        Send(new { action = "configure", config = updated });
    }
    private void BrowseOutput_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog { Title = "音楽の保存先を選択", InitialDirectory = Directory.Exists(OutputInput.Text) ? OutputInput.Text : "" };
        if (dialog.ShowDialog(this) == true) OutputInput.Text = dialog.FolderName;
    }
    private void BrowseFfmpeg_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog { Title = "ffmpeg.exeを選択", Filter = "FFmpeg|ffmpeg.exe|実行ファイル|*.exe" };
        if (dialog.ShowDialog(this) == true) FfmpegInput.Text = dialog.FileName;
    }
    private void Disconnect_Click(object sender, RoutedEventArgs e)
    {
        if (ShowDialog("Spotifyの接続を解除", "保存されているSpotifyの認証情報を削除します。公開リンクは引き続き利用できます。", "解除する")) Send(new { action = "disconnect" });
    }
    private void Connect_Click(object sender, RoutedEventArgs e) => StartCommand(new { action = "connect" }, "connect", "ブラウザーでSpotifyへの接続を許可してください。");
    private void Update_Click(object sender, RoutedEventArgs e) => StartCommand(new { action = "check_update", silent = false }, "check_update", "更新を確認中…");
    private void Folder_Click(object sender, RoutedEventArgs e)
    {
        if (outputDir.Length == 0) return;
        try { Directory.CreateDirectory(outputDir); Process.Start(new ProcessStartInfo("explorer.exe") { ArgumentList = { outputDir }, UseShellExecute = true }); }
        catch (Exception ex) { ShowError(ex.Message); }
    }
    private void OpenLink(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || uri.Scheme != "https") return;
        try { Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true }); }
        catch (Exception ex) { ShowError(ex.Message); }
    }

    private void ShowError(string message)
    {
        StatusText.Text = message.Replace('\n', ' '); SettingsStatus.Text = "操作を完了できませんでした。";
        ShowDialog("操作を完了できませんでした", message);
    }

    private bool ShowDialog(string title, string message, string? confirm = null, FrameworkElement? extra = null)
    {
        var dialog = new Window { Owner = this, Title = title, Width = 540, SizeToContent = SizeToContent.Height,
            MaxHeight = Math.Max(400, SystemParameters.WorkArea.Height - 100), ResizeMode = ResizeMode.NoResize,
            WindowStartupLocation = WindowStartupLocation.CenterOwner, ShowInTaskbar = false,
            FontFamily = FontFamily, FontSize = 13 };
        var grid = new Grid { Margin = new Thickness(26) };
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.Children.Add(new TextBlock { Text = title, FontSize = 22, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 16) });
        var body = new StackPanel(); body.Children.Add(new TextBlock { Text = message, TextWrapping = TextWrapping.Wrap, LineHeight = 22 });
        if (extra != null) body.Children.Add(extra);
        var scroll = new ScrollViewer { Content = body, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, MaxHeight = SystemParameters.WorkArea.Height - 310 };
        Grid.SetRow(scroll, 1); grid.Children.Add(scroll);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 24, 0, 0) };
        var cancel = new Button { Content = confirm == null ? "閉じる" : "キャンセル", IsCancel = true, Style = (Style)FindResource("Action") };
        System.Windows.Automation.AutomationProperties.SetAutomationId(cancel, "DialogCancel");
        cancel.Click += (_, _) => dialog.Close(); buttons.Children.Add(cancel);
        if (confirm != null)
        {
            var accept = new Button { Content = confirm, IsDefault = true, Style = (Style)FindResource("Primary"), Margin = new Thickness(8, 0, 0, 0) };
            System.Windows.Automation.AutomationProperties.SetAutomationId(accept, "DialogConfirm");
            accept.Click += (_, _) => { dialog.DialogResult = true; }; buttons.Children.Add(accept);
        }
        Grid.SetRow(buttons, 2); grid.Children.Add(buttons); dialog.Content = grid;
        return dialog.ShowDialog() == true;
    }

    private void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (busy && !ShowDialog("アプリを終了", "進行中の処理を停止して終了しますか？", "終了する")) { e.Cancel = true; return; }
        closing = true; backend.Dispose();
    }

    private void LoadPreview()
    {
        // Isolated visual QA: no network requests, credentials, or writes to user settings.
        Title += " — UI Preview";
        ready = true; StatusText.Text = "リンクを読み込んで、はじめましょう。";
        outputDir = @"C:\Users\Music\Playlist Audio Saver"; OutputText.Text = "保存先  ·  " + outputDir;
        ConnectionText.Text = "公開リンクを利用できます"; VersionText.Text = "UI Preview";
        config = JsonNode.Parse("""{"client_id":"","output_dir":"","ffmpeg_path":"","market":"JP","audio_quality":"0","search_results":5,"skip_existing":true,"check_updates":false,"redirect_port":43821}""")!.AsObject();
        FillSettings(); UpdateActions();
        var args = Environment.GetCommandLineArgs();
        if (args.Contains("--populated"))
        {
            var names = new[] { "夜を歩く", "Blue Hour", "雨のち晴れ", "Somewhere Only We Know", "明日への手紙", "Sunday Morning", "長い曲名の表示確認 — Live Session at the Riverside", "星の降る夜に" };
            for (var i = 0; i < names.Length; i++) Tracks.Add(new TrackRow { Index = i, Position = i + 1, Name = names[i], Subtitle = "Sample Artist  ·  3:42", VideoTitle = names[i] + " (Official Audio)", VideoSubtitle = "Sample Artist  ·  3:44", VideoUrl = "https://www.youtube.com/", Status = i == 2 ? "要確認" : i == 4 ? "除外" : "対応候補", Excluded = i == 4 });
            CollectionTitle.Text = "夜のドライブ"; CollectionInfo.Text = "プレイリスト  ·  8 曲  ·  UI Preview";
            EmptyState.Visibility = Visibility.Collapsed; TrackGrid.Visibility = Visibility.Visible;
            mappingReady = true; TrackGrid.SelectedIndex = 2; Progress.Value = 100;
            StatusText.Text = "検索完了。候補を確認して保存してください。"; UpdateActions();
        }
        if (args.Contains("--settings")) { SettingsNav.IsChecked = true; Settings_Click(this, new RoutedEventArgs()); }
        if (args.Contains("--compact")) { Width = 980; Height = 690; }
        var capture = Array.IndexOf(args, "--capture");
        if (capture >= 0 && capture + 1 < args.Length)
        {
            var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            timer.Tick += (_, _) =>
            {
                timer.Stop(); UpdateLayout();
                var bitmap = new RenderTargetBitmap((int)ActualWidth, (int)ActualHeight, 96, 96, PixelFormats.Pbgra32);
                bitmap.Render(this);
                var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap));
                using (var file = File.Create(args[capture + 1])) encoder.Save(file);
                Close();
            };
            timer.Start();
        }
    }
}
