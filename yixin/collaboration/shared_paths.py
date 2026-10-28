from __future__ import annotations

import re
from pathlib import Path

from .types import ProjectTask


_INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_path_component(value: str, fallback: str = "未命名") -> str:
    clean = _INVALID_COMPONENT.sub("_", value.strip()).strip(" .")
    return clean[:120] or fallback


class SharedPathResolver:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def resolve(self, relative_path: str | Path) -> Path:
        candidate = (self.root / Path(relative_path)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("共享盘路径不能超出配置的共享根目录") from exc
        return candidate

    def project_root(self, project_relative_root: str) -> Path:
        return self.resolve(project_relative_root)

    def resolve_project_path(
        self,
        project_relative_root: str,
        item_relative_path: str,
    ) -> Path:
        project_root = self.project_root(project_relative_root)
        candidate = self.resolve(Path(project_relative_root) / item_relative_path)
        try:
            candidate.relative_to(project_root)
        except ValueError as exc:
            raise ValueError("项目文件路径不能超出当前项目目录") from exc
        return candidate

    def relative_to_project(self, project_root: Path, path: Path) -> str:
        project = project_root.resolve()
        candidate = path.expanduser().resolve()
        try:
            relative = candidate.relative_to(project)
        except ValueError as exc:
            raise ValueError("图片必须位于项目目录内") from exc
        return relative.as_posix()

    def relative_to_root(self, path: Path) -> str:
        candidate = path.expanduser().resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("项目目录必须位于共享盘根目录内") from exc
        return relative.as_posix()

    def output_directory(
        self,
        task: ProjectTask,
        attempt_no: int,
    ) -> Path:
        project_root = self.project_root(task.project_relative_root)
        return project_root / "AI成品"

    def relative_output_path(self, task: ProjectTask, path: Path) -> str:
        return self.relative_to_project(
            self.project_root(task.project_relative_root),
            path,
        )

    def resolve_output(self, project_relative_root: str, relative_path: str) -> Path:
        return self.resolve_project_path(project_relative_root, relative_path)
