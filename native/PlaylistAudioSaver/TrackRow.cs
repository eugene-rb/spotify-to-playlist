using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace PlaylistAudioSaver;

public sealed class TrackRow : INotifyPropertyChanged
{
    public int Index { get; set; }
    public int Position { get; set; }
    public string Name { get; set; } = "";
    public string Subtitle { get; set; } = "";
    public string VideoTitle { get; set; } = "未検索";
    public string VideoSubtitle { get; set; } = "候補を検索してください";
    public string SpotifyUrl { get; set; } = "";
    public string VideoUrl { get; set; } = "";
    public string Status { get; set; } = "未検索";
    public string Detail { get; set; } = "";
    public bool Excluded { get; set; }
    public ImageSource? Thumbnail { get; set; }
    public event PropertyChangedEventHandler? PropertyChanged;

    public void Update(JsonElement data, JsonElement envelope = default)
    {
        Position = data.GetProperty("position").GetInt32();
        Name = data.Text("name");
        Subtitle = $"{data.Text("artist_text")}  ·  {data.Text("duration_text")}";
        var previousUrl = VideoUrl;
        SpotifyUrl = data.Text("spotify_url");
        VideoUrl = data.Text("selected_video_url");
        VideoTitle = data.Text("selected_video_title") is { Length: > 0 } title ? title : "未検索";
        VideoSubtitle = VideoUrl.Length > 0 ? $"{data.Text("selected_video_uploader")}  ·  {data.Text("youtube_duration_text")}" : "候補を検索してください";
        Excluded = data.Flag("excluded");
        Status = Excluded ? "除外" : data.Text("status");
        if (previousUrl != VideoUrl || Status == "検索待ち") { Thumbnail = null; Detail = ""; }
        if (envelope.ValueKind == JsonValueKind.Object)
        {
            if (envelope.TryGetProperty("detail", out var detail)) Detail = detail.GetString() ?? "";
            if (envelope.TryGetProperty("thumbnail", out var thumbnail))
            {
                Thumbnail = null;
                try
                {
                    if (!string.IsNullOrEmpty(thumbnail.GetString()))
                    {
                        using var stream = new MemoryStream(Convert.FromBase64String(thumbnail.GetString()!));
                        var bitmap = new BitmapImage();
                        bitmap.BeginInit(); bitmap.CacheOption = BitmapCacheOption.OnLoad;
                        bitmap.DecodePixelWidth = 144; bitmap.StreamSource = stream; bitmap.EndInit(); bitmap.Freeze();
                        Thumbnail = bitmap;
                    }
                }
                catch (Exception ex) when (ex is FormatException or IOException or NotSupportedException or ArgumentException) { }
            }
        }
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(null));
    }
}

internal static class JsonExtensions
{
    public static string Text(this JsonElement element, string name) => element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString()! : "";
    public static bool Flag(this JsonElement element, string name) => element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.True;
}
