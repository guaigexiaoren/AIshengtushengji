from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def executable_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    bundled_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundled_root) if bundled_root else Path(__file__).resolve().parent.parent
    return root.joinpath(*parts)


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @classmethod
    def default(cls) -> "AppPaths":
        override = os.environ.get("YIXIN_DATA_DIR")
        root = Path(override).expanduser().resolve() if override else executable_root() / "data"
        return cls(root)

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def temp(self) -> Path:
        return self.root / "temp"

    @property
    def library(self) -> Path:
        return self.root / "library"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def database(self) -> Path:
        return self.root / "app.db"

    @property
    def api_key_file(self) -> Path:
        return self.config / "api_key.bin"

    @property
    def database_config_file(self) -> Path:
        return self.config / "database.bin"

    @property
    def collaboration_marker(self) -> Path:
        return self.config / "collaboration_upgrade_v1"

    @property
    def log_file(self) -> Path:
        return self.logs / "app.log"

    def ensure(self) -> None:
        for directory in (self.config, self.temp, self.library, self.logs):
            directory.mkdir(parents=True, exist_ok=True)

    def is_under(self, path: Path, base: Path) -> bool:
        try:
            path.resolve().relative_to(base.resolve())
            return True
        except ValueError:
            return False
