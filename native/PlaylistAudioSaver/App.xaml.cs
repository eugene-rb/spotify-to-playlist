using System.Windows;

namespace PlaylistAudioSaver;

public partial class App : Application
{
    public App()
    {
        var args = Environment.GetCommandLineArgs();
        ThemeMode = args.Contains("--dark") ? ThemeMode.Dark : args.Contains("--light") ? ThemeMode.Light : ThemeMode.System;
    }
}
