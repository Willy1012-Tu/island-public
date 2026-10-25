"""Small, versioned desktop settings; no model credentials or automatic jobs."""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("island.desktop")


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "IslandWorldDesktop"


@dataclass(frozen=True)
class DesktopConfig:
    workspace: str = str(PROJECT_ROOT)
    start_minimized: bool = False
    close_to_tray: bool = True
    window_width: int = 1440
    window_height: int = 940
    port: int = 0
    monitor_timeout: float = 300.0

    @classmethod
    def from_dict(cls, data: object) -> "DesktopConfig":
        if not isinstance(data, dict):
            raise ValueError("Desktop settings must be an object.")
        defaults = cls()
        workspace = data.get("workspace", defaults.workspace)
        if not isinstance(workspace, str) or not workspace or not Path(workspace).is_dir():
            raise ValueError("Desktop workspace must be an existing directory.")
        values = {"workspace": str(Path(workspace).resolve())}
        for field in ("start_minimized", "close_to_tray"):
            value = data.get(field, getattr(defaults, field))
            if type(value) is not bool:
                raise ValueError(f"{field} must be boolean.")
            values[field] = value
        for field, lower, upper in (("window_width", 780, 7680), ("window_height", 560, 4320), ("port", 0, 65535)):
            value = data.get(field, getattr(defaults, field))
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{field} is outside its allowed range.")
            values[field] = value
        timeout = data.get("monitor_timeout", defaults.monitor_timeout)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
            raise ValueError("monitor_timeout must be a finite positive number, at most one day.")
        values["monitor_timeout"] = float(timeout)
        return cls(**values)


class SettingsStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / "settings.json"

    def load(self) -> DesktopConfig:
        if not self.path.exists():
            return DesktopConfig()
        try:
            if self.path.stat().st_size > 64 * 1024:
                raise ValueError("Settings file is too large.")
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(document, dict) or type(document.get("version")) is not int
                    or document["version"] != 1):
                raise ValueError("Unsupported desktop settings version.")
            return DesktopConfig.from_dict(document.get("settings"))
        except (OSError, ValueError, TypeError):
            LOGGER.warning("Desktop settings could not be loaded; using defaults and preserving the file.")
            return DesktopConfig()

    def save(self, config: DesktopConfig) -> None:
        validated = DesktopConfig.from_dict(asdict(config))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.data_dir,
                                             prefix=".settings-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump({"version": 1, "settings": asdict(validated)}, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
