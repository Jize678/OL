"""应用配置。配置目录放在 %APPDATA%\\openLight，便于卸载/清理。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_DIR_NAME = "openLight"

DEFAULT_API_PORT = 8765
DEFAULT_PREVIEW_MAX_SIDE = 1400
DEFAULT_EXPORT_SUFFIX = "_openlight"


def app_data_dir() -> Path:
    """配置目录。优先 %APPDATA%\\openLight，不可写时退回临时目录。"""
    override = os.environ.get("OPENLIGHT_HOME")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    candidates.append(Path(base) / APP_DIR_NAME)
    candidates.append(Path(tempfile.gettempdir()) / APP_DIR_NAME)

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except Exception:
            continue
    raise RuntimeError("找不到可写的配置目录。")


@dataclass
class Settings:
    api_host: str = "127.0.0.1"
    api_port: int = DEFAULT_API_PORT
    api_enabled: bool = True
    preview_max_side: int = DEFAULT_PREVIEW_MAX_SIDE
    export_suffix: str = DEFAULT_EXPORT_SUFFIX
    jpeg_quality: int = 95
    last_open_dir: str = ""
    theme: str = "dark"
    extra: dict = field(default_factory=dict)

    @property
    def settings_path(self) -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        path = app_data_dir() / "settings.json"
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in raw.items() if k in known}
        return cls(**data)

    def save(self) -> None:
        self.settings_path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
