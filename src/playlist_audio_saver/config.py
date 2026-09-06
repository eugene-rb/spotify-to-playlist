from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


APP_NAME = "PlaylistAudioSaver"


def config_directory() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / APP_NAME


def default_output_directory() -> Path:
    music = Path.home() / "Music"
    return music / "Playlist Audio Saver"


@dataclass(slots=True)
class AppConfig:
    client_id: str = ""
    output_dir: str = ""
    market: str = "JP"
    ffmpeg_path: str = ""
    audio_quality: str = "0"
    search_results: int = 5
    skip_existing: bool = True
    check_updates: bool = True
    redirect_port: int = 43821

    @property
    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir).expanduser() if self.output_dir else default_output_directory()


class ConfigStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or config_directory()
        self.path = self.directory / "config.json"

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig(output_dir=str(default_output_directory()))
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            valid = {field for field in AppConfig.__dataclass_fields__}
            return AppConfig(**{key: value for key, value in raw.items() if key in valid})
        except (OSError, ValueError, TypeError):
            return AppConfig(output_dir=str(default_output_directory()))

    def save(self, config: AppConfig) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8"
        )
