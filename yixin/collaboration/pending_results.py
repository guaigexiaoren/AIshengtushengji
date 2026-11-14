from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..storage import atomic_write_bytes


@dataclass(frozen=True)
class PendingResult:
    attempt_id: int
    task_id: int
    project_relative_root: str
    task_code: str
    attempt_no: int
    result_index: int
    path: Path
    content_hash: str
    mime: str
    width: int
    height: int


def pending_results_root(temp_root: Path) -> Path:
    return Path(temp_root) / "pending_results"


def _attempt_root(temp_root: Path, attempt_id: int) -> Path:
    return pending_results_root(temp_root) / str(int(attempt_id))


def _manifest_path(temp_root: Path, attempt_id: int) -> Path:
    return _attempt_root(temp_root, attempt_id) / "manifest.json"


def save_pending_manifest(
    temp_root: Path,
    *,
    attempt_id: int,
    task_id: int,
    project_relative_root: str,
    task_code: str,
    attempt_no: int,
    items: list[dict],
) -> None:
    folder = _attempt_root(temp_root, attempt_id)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "attempt_id": int(attempt_id),
        "task_id": int(task_id),
        "project_relative_root": project_relative_root,
        "task_code": task_code,
        "attempt_no": int(attempt_no),
        "items": items,
    }
    atomic_write_bytes(
        folder / "manifest.json",
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def load_pending_results(
    temp_root: Path,
    attempt_id: int,
) -> list[PendingResult]:
    path = _manifest_path(temp_root, attempt_id)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    folder = _attempt_root(temp_root, attempt_id)
    results: list[PendingResult] = []
    for item in data.get("items", []):
        file_path = folder / str(item.get("filename") or "")
        if not item.get("filename") or not file_path.is_file():
            continue
        results.append(
            PendingResult(
                attempt_id=int(data.get("attempt_id") or attempt_id),
                task_id=int(data.get("task_id") or 0),
                project_relative_root=str(
                    data.get("project_relative_root") or ""
                ),
                task_code=str(data.get("task_code") or ""),
                attempt_no=int(data.get("attempt_no") or 1),
                result_index=int(item.get("result_index") or 0),
                path=file_path,
                content_hash=str(item.get("content_hash") or ""),
                mime=str(item.get("mime") or ""),
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
            )
        )
    return results


def clear_pending_results(temp_root: Path, attempt_id: int) -> None:
    folder = _attempt_root(temp_root, attempt_id)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def migrate_legacy_pending_results(temp_root: Path) -> None:
    """修复旧版本把暂存目录写成 pending_results/pending_results 的数据。"""
    root = pending_results_root(temp_root)
    legacy = root / "pending_results"
    if not legacy.is_dir():
        return
    for child in legacy.iterdir():
        target = root / child.name
        if target.exists():
            continue
        try:
            child.rename(target)
        except OSError:
            continue
    try:
        legacy.rmdir()
    except OSError:
        pass


def remove_pending_item(
    temp_root: Path,
    attempt_id: int,
    result_index: int,
) -> None:
    path = _manifest_path(temp_root, attempt_id)
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    folder = _attempt_root(temp_root, attempt_id)
    remaining = []
    for item in data.get("items", []):
        if int(item.get("result_index") or 0) == int(result_index):
            file_path = folder / str(item.get("filename") or "")
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        remaining.append(item)
    data["items"] = remaining
    if remaining:
        atomic_write_bytes(
            path,
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return
    try:
        path.unlink(missing_ok=True)
        folder.rmdir()
    except OSError:
        pass
