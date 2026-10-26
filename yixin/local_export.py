from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .database import Database
from .settings import (
    LOCAL_SAVE_FIXED,
    LOCAL_SAVE_REFERENCE,
    AppSettings,
)
from .storage import atomic_copy_file


def export_original_image(
    source: Path,
    directory: Path,
    *,
    task_code: str,
    result_index: int,
) -> Path:
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise ValueError("生成结果原文件已不存在")
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"AI图_{timestamp}_{task_code}_{result_index + 1}"
    suffix = source.suffix or ".png"
    destination = directory / f"{stem}{suffix}"
    sequence = 2
    while destination.exists():
        destination = directory / f"{stem}_{sequence}{suffix}"
        sequence += 1
    atomic_copy_file(source, destination)
    return destination


class LocalImageExporter:
    def __init__(self, database: Database, settings: AppSettings) -> None:
        self.database = database
        self.settings = settings

    def preferred_directory(self, task_id: int) -> Path | None:
        mode = self.settings.local_save_mode()
        if mode == LOCAL_SAVE_FIXED:
            folder = self.settings.fixed_folder()
            return folder if folder and folder.is_dir() else None
        if mode == LOCAL_SAVE_REFERENCE:
            references = self.database.get_task_references(task_id)
            if references:
                source = references[-1].source_path
                if source and source.is_file():
                    source = source.resolve()
                    base = source.parent.parent
                    if base.is_dir():
                        folder = base / "AI图"
                        try:
                            folder.mkdir(exist_ok=True)
                        except OSError:
                            pass
                        else:
                            if folder.is_dir():
                                return folder
        last_folder = self.settings.last_folder()
        return last_folder if last_folder and last_folder.is_dir() else None

    def save_result(self, task_id: int, result_id: int, directory: Path) -> Path:
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        results = self.database.get_task_results(task_id)
        result = next((item for item in results if int(item["id"]) == result_id), None)
        if result is None:
            raise ValueError("生成结果不存在")
        source = Path(result["path"])
        if not source.is_file():
            raise ValueError("生成结果文件已不存在")
        directory = directory.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise ValueError("本地保存位置不是有效文件夹")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"AI图_{timestamp}_{int(result['position']) + 1}"
        suffix = source.suffix or ".png"
        destination = directory / f"{stem}{suffix}"
        sequence = 2
        while destination.exists():
            destination = directory / f"{stem}_{sequence}{suffix}"
            sequence += 1
        atomic_copy_file(source, destination)
        self.settings.set_last_folder(directory)
        return destination
