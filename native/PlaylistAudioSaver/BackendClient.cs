using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace PlaylistAudioSaver;

internal sealed class BackendClient : IDisposable
{
    private Process? process;
    private bool stopping;
    private readonly StringBuilder errors = new();
    public event Action<JsonElement>? Message;
    public event Action<string>? Failed;

    public void Start()
    {
        var bundled = Path.Combine(AppContext.BaseDirectory, "backend", "PlaylistAudioBackend.exe");
        var start = new ProcessStartInfo
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        if (File.Exists(bundled)) start.FileName = bundled;
        else
        {
            var root = FindWorkspace();
            start.FileName = Path.Combine(root, ".venv", "Scripts", "python.exe");
            if (!File.Exists(start.FileName)) throw new FileNotFoundException("Python環境がありません。scripts/setup.ps1を実行してください。");
            start.WorkingDirectory = root;
            start.ArgumentList.Add("-m");
            start.ArgumentList.Add("playlist_audio_saver.backend");
        }
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        start.Environment["PYTHONUNBUFFERED"] = "1";
        start.Environment["PLAYLIST_SAVER_HOST"] = Environment.ProcessPath!;
        start.Environment["PLAYLIST_SAVER_HOST_PID"] = Environment.ProcessId.ToString();
        process = new Process { StartInfo = start, EnableRaisingEvents = true };
        process.OutputDataReceived += (_, e) =>
        {
            if (e.Data is null || stopping) return;
            try { using var doc = JsonDocument.Parse(e.Data); Message?.Invoke(doc.RootElement.Clone()); }
            catch (JsonException) { /* third-party diagnostics never become UI commands */ }
        };
        process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is null) return;
            lock (errors) { errors.AppendLine(e.Data); if (errors.Length > 4000) errors.Remove(0, errors.Length - 4000); }
        };
        process.Exited += (_, _) =>
        {
            if (!stopping) Failed?.Invoke("音楽処理が終了しました。アプリを起動し直してください。\n" + errors);
        };
        process.Start();
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
    }

    private static string FindWorkspace()
    {
        foreach (var starting in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
            for (var path = new DirectoryInfo(starting); path != null; path = path.Parent)
                if (File.Exists(Path.Combine(path.FullName, "pyproject.toml"))) return path.FullName;
        throw new DirectoryNotFoundException("アプリのバックエンドが見つかりません。再インストールしてください。");
    }

    public void Send(object message)
    {
        if (process is null || process.HasExited) throw new IOException("音楽処理に接続できません。アプリを起動し直してください。");
        process.StandardInput.WriteLine(JsonSerializer.Serialize(message));
        process.StandardInput.Flush();
    }

    public void Dispose()
    {
        stopping = true;
        if (process is null) return;
        try
        {
            if (!process.HasExited)
            {
                Send(new { action = "shutdown" });
                // Killing the tree also closes any FFmpeg child left by an interrupted save.
                if (!process.WaitForExit(800)) process.Kill(entireProcessTree: true);
            }
        }
        catch (InvalidOperationException) { }
        catch (IOException) { if (!process.HasExited) process.Kill(entireProcessTree: true); }
        finally { process.Dispose(); }
    }
}
