"""Launch the Windows native desktop shell from Python entry points."""
from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Playlist Audio Saver requires Windows 11.")
    root = Path(__file__).resolve().parents[2]
    candidates = [
        root / "native/PlaylistAudioSaver/bin/Release/net10.0-windows/PlaylistAudioSaver.exe",
        root / "dist/PlaylistAudioSaver/PlaylistAudioSaver.exe",
    ]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        ctypes.windll.user32.MessageBoxW(None, "先に scripts\\setup.ps1 を実行してWindows GUIをビルドしてください。", "Playlist Audio Saver", 0x10)
        return
    subprocess.Popen([str(executable)], cwd=root, close_fds=True)


if __name__ == "__main__":
    main()
