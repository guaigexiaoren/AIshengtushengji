from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import Database
from .models import StoredImage
from .paths import AppPaths
from .storage import (
    SUPPORTED_IMAGE_SUFFIXES,
    FileStore,
    image_metadata,
    sha256_file,
)


@dataclass(frozen=True)
class LibraryImportSummary:
    work_ids: tuple[int, ...]
    errors: tuple[str, ...]


class LibraryService:
    def __init__(
        self,
        database: Database,
        file_store: FileStore,
        paths: AppPaths,
    ) -> None:
        self.database = database
        self.file_store = file_store
        self.paths = paths

    def import_image(self, source: Path) -> int:
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise ValueError("图片文件不存在")
        if source.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            raise ValueError("暂不支持这种图片格式")

        folder_name = uuid.uuid4().hex
        folder = self.file_store.library_folder(folder_name)
        folder.mkdir(parents=True, exist_ok=False)
        work_id: int | None = None
        try:
            output_path = self.file_store.copy_to_library(
                folder,
                source,
                "output",
            )
            mime, width, height = image_metadata(output_path.read_bytes())
            output_stored = StoredImage(
                path=output_path,
                content_hash=sha256_file(output_path),
                mime=mime,
                width=width,
                height=height,
                original_name=source.name,
            )
            thumbnail_path = self.file_store.create_thumbnail(
                output_path,
                folder / "thumbnail.jpg",
            )
            thumbnail = StoredImage(
                path=thumbnail_path,
                content_hash=sha256_file(thumbnail_path),
                mime="image/jpeg",
                width=0,
                height=0,
                original_name="thumbnail.jpg",
            )
            title = source.stem.strip()[:48] or "手动上传作品"
            work_id = self.database.create_manual_work_with_assets(
                folder_name=folder_name,
                title=title,
                parameters={
                    "source": "manual_upload",
                    "width": width,
                    "height": height,
                },
                assets=(
                    ("result", 0, output_stored),
                    ("thumbnail", 0, thumbnail),
                ),
            )
            return work_id
        except Exception:
            if work_id is not None:
                try:
                    self.database.delete_work(work_id)
                except Exception:
                    pass
            try:
                self.file_store.remove_work_folder(folder)
            except Exception:
                pass
            raise

    def import_images(self, paths: Sequence[Path]) -> LibraryImportSummary:
        work_ids: list[int] = []
        errors: list[str] = []
        for path in paths:
            try:
                work_ids.append(self.import_image(path))
            except Exception as exc:
                errors.append(f"{Path(path).name}：{exc}")
        return LibraryImportSummary(tuple(work_ids), tuple(errors))

    def save_task_result(self, task_id: int, result_id: int) -> int:
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        results = self.database.get_task_results(task_id)
        result = next((item for item in results if int(item["id"]) == result_id), None)
        if result is None:
            raise ValueError("生成结果不存在")
        if result["saved"]:
            raise ValueError("这张图片已经保存到作品库")
        result_path = Path(result["path"])
        if not result_path.is_file():
            raise ValueError("临时图片已不存在，无法保存")

        folder_name = uuid.uuid4().hex
        folder = self.file_store.library_folder(folder_name)
        folder.mkdir(parents=True, exist_ok=False)
        work_id: int | None = None
        try:
            output_path = self.file_store.copy_to_library(
                folder, result_path, "output"
            )
            output_mime, output_width, output_height = image_metadata(
                output_path.read_bytes()
            )
            output_stored = StoredImage(
                path=output_path,
                content_hash=result["content_hash"],
                mime=output_mime,
                width=output_width,
                height=output_height,
                original_name=result_path.name,
            )
            thumbnail_temp = self.file_store.create_thumbnail(
                output_path, folder / "thumbnail.jpg"
            )
            thumbnail_image = StoredImage(
                path=thumbnail_temp,
                content_hash=sha256_file(thumbnail_temp),
                mime="image/jpeg",
                width=0,
                height=0,
                original_name="thumbnail.jpg",
            )
            reference_assets: list[StoredImage] = []
            for position, reference in enumerate(
                self.database.get_task_references(task_id), start=1
            ):
                if not reference.path.is_file():
                    raise ValueError(f"第 {position} 张参考图已不存在")
                copied = self.file_store.copy_to_library(
                    folder, reference.path, f"reference_{position:02d}"
                )
                reference_assets.append(
                    StoredImage(
                        path=copied,
                        content_hash=reference.content_hash,
                        mime=reference.mime,
                        width=reference.width,
                        height=reference.height,
                        original_name=reference.original_name,
                    )
                )

            parameters: dict[str, Any] = {
                "mode": task["mode"],
                "model": task["model"],
                "aspect_ratio": task["aspect_ratio"],
                "resolution": task["resolution"],
                "n": task["n"],
            }
            if task["auto_strategy"]:
                parameters["auto_strategy"] = task["auto_strategy"]
                parameters["auto_reason"] = task["auto_reason"]
            title = task["prompt"].strip().splitlines()[0][:48] or "未命名作品"
            assets: list[tuple[str, int, StoredImage]] = [
                ("result", 0, output_stored),
                ("thumbnail", 0, thumbnail_image),
            ]
            assets.extend(
                ("reference", position, reference)
                for position, reference in enumerate(reference_assets)
            )
            work_id = self.database.create_work_with_assets(
                task_id=task_id,
                folder_name=folder_name,
                title=title,
                prompt=task["prompt"],
                model=task["model"],
                parameters=parameters,
                raw_response=task["raw_response"],
                assets=assets,
                saved_result_id=result_id,
                saved_path=output_path,
                saved_thumbnail_path=thumbnail_temp,
                saved_mime=output_mime,
                saved_width=output_width,
                saved_height=output_height,
            )
            return work_id
        except Exception:
            if work_id is not None:
                try:
                    self.database.delete_work(work_id)
                except Exception:
                    pass
            try:
                self.file_store.remove_work_folder(folder)
            except Exception:
                pass
            raise

    def save_project_result(
        self,
        *,
        repository,
        resolver,
        task_id: int,
        attempt_id: int,
        result,
        reference_snapshots: Sequence[StoredImage] | None = None,
    ) -> int:
        task = repository.get_task(task_id)
        if task is None:
            raise ValueError("项目子任务不存在")
        attempt = next(
            (
                item
                for item in repository.list_attempts(task_id)
                if item.id == attempt_id
            ),
            None,
        )
        if attempt is None:
            raise ValueError("项目生成轮次不存在")
        result_path = resolver.resolve_project_path(
            task.project_relative_root,
            result.relative_path,
        )
        if not result_path.is_file():
            raise ValueError("项目成品文件已不存在")
        folder_name = uuid.uuid4().hex
        folder = self.file_store.library_folder(folder_name)
        folder.mkdir(parents=True, exist_ok=False)
        work_id: int | None = None
        try:
            output_path = self.file_store.copy_to_library(
                folder,
                result_path,
                "output",
            )
            output_mime, output_width, output_height = image_metadata(
                output_path.read_bytes()
            )
            output_stored = StoredImage(
                path=output_path,
                content_hash=result.content_hash,
                mime=output_mime,
                width=output_width,
                height=output_height,
                original_name=result_path.name,
            )
            thumbnail_path = self.file_store.create_thumbnail(
                output_path,
                folder / "thumbnail.jpg",
            )
            thumbnail = StoredImage(
                path=thumbnail_path,
                content_hash=sha256_file(thumbnail_path),
                mime="image/jpeg",
                width=0,
                height=0,
                original_name="thumbnail.jpg",
            )
            references: list[StoredImage] = []
            if reference_snapshots is not None:
                reference_assets = [
                    (
                        Path(image.path),
                        image.original_name,
                        image.content_hash,
                        image.mime,
                        image.width,
                        image.height,
                    )
                    for image in reference_snapshots
                ]
            else:
                reference_assets = [
                    (
                        resolver.resolve_project_path(
                            task.project_relative_root,
                            asset.relative_path,
                        ),
                        asset.original_name,
                        asset.content_hash,
                        asset.mime,
                        asset.width,
                        asset.height,
                    )
                    for asset in task.garment_assets
                ]
                model_path = resolver.resolve_project_path(
                    task.project_relative_root,
                    task.model_path,
                )
                if not model_path.is_file():
                    raise ValueError(
                        f"项目模特参考图已不存在：{model_path}"
                    )
                model_mime, model_width, model_height = image_metadata(
                    model_path.read_bytes()
                )
                reference_assets.append(
                    (
                        model_path,
                        task.model_name,
                        task.model_hash,
                        model_mime,
                        model_width,
                        model_height,
                    )
                )
            for position, (
                source,
                original_name,
                content_hash,
                mime,
                width,
                height,
            ) in enumerate(reference_assets, start=1):
                if not source.is_file():
                    raise ValueError(
                        f"第 {position} 张项目参考图已不存在"
                    )
                if sha256_file(source) != content_hash:
                    raise ValueError(
                        f"第 {position} 张项目参考图内容已变化"
                    )
                copied = self.file_store.copy_to_library(
                    folder,
                    source,
                    f"reference_{position:02d}",
                )
                references.append(
                    StoredImage(
                        path=copied,
                        content_hash=content_hash,
                        mime=mime,
                        width=width,
                        height=height,
                        original_name=original_name,
                    )
                )
            prompt = attempt.prompt_text or task.prompt_text
            parameters = {
                "source": "project_task",
                "project_code": task.project_code,
                "task_code": task.task_code,
                "attempt_no": attempt.attempt_no,
                "model": attempt.model,
                "aspect_ratio": attempt.aspect_ratio,
                "resolution": attempt.resolution,
                "n": attempt.n,
                "provider_code": attempt.provider_code,
                "provider_name": attempt.provider_name,
                "requested_provider_code":
                    attempt.requested_provider_code,
                "requested_provider_name":
                    attempt.requested_provider_name,
            }
            assets = [
                ("result", 0, output_stored),
                ("thumbnail", 0, thumbnail),
                *(
                    ("reference", position, image)
                    for position, image in enumerate(references)
                ),
            ]
            title = (
                prompt.strip().splitlines()[0][:48]
                if prompt.strip()
                else task.task_code
            )
            work_id = self.database.create_external_work_with_assets(
                folder_name=folder_name,
                title=title,
                prompt=prompt,
                model=attempt.model,
                parameters=parameters,
                assets=assets,
            )
            return work_id
        except Exception:
            if work_id is not None:
                try:
                    self.database.delete_work(work_id)
                except Exception:
                    pass
            try:
                self.file_store.remove_work_folder(folder)
            except Exception:
                pass
            raise

    def delete_work(self, work_id: int) -> bool:
        work = self.database.get_work(work_id)
        if work is None:
            return False
        task_id = work.get("task_id")
        result_assets = self.database.get_work_assets(
            work_id,
            role="result",
        )
        if task_id is not None and result_assets:
            saved_path = Path(result_assets[0]["path"])
            if saved_path.is_file():
                data = saved_path.read_bytes()
                stored = self.file_store.store_generated(
                    int(task_id),
                    data,
                    0,
                )
                thumbnail = self.file_store.create_thumbnail(
                    stored.path,
                )
                restored = self.database.restore_saved_task_result(
                    int(task_id),
                    saved_path,
                    stored,
                    thumbnail,
                )
                if not restored:
                    raise ValueError(
                        "历史生成结果状态异常，未删除作品"
                    )
            else:
                # 成品文件已丢失：把结果恢复为未保存，避免删除后永久无法再保存。
                self.database.release_saved_task_result(
                    int(task_id),
                    saved_path,
                )
        deleted = self.database.delete_work(work_id)
        if deleted is None:
            return False
        folder_name = deleted["work"]["folder_name"]
        self.file_store.remove_work_folder(self.file_store.library_folder(folder_name))
        return True

    def work_folder(self, work_id: int) -> Path:
        work = self.database.get_work(work_id)
        if work is None:
            raise ValueError("作品不存在")
        return self.file_store.library_folder(work["folder_name"])

    def work_parameters(self, work: dict[str, Any]) -> dict[str, Any]:
        try:
            return json.loads(work.get("parameters_json") or "{}")
        except json.JSONDecodeError:
            return {}
