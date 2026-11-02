from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..models import StoredImage
from ..storage import (
    SUPPORTED_IMAGE_SUFFIXES,
    atomic_copy_file,
    atomic_write_bytes,
    image_metadata,
    sha256_bytes,
)
from .shared_paths import SharedPathResolver
from .types import ProjectTask


@dataclass(frozen=True)
class LocalSourceState:
    source_revision: int
    garments: tuple[StoredImage, ...]
    model: StoredImage | None

    def references(self) -> tuple[StoredImage, ...]:
        return self.garments + ((self.model,) if self.model is not None else ())


class LocalTaskSourceStore:
    def __init__(
        self,
        root: Path,
        actor_id: int,
        task: ProjectTask,
        resolver: SharedPathResolver,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.actor_id = int(actor_id)
        self.task = task
        self.resolver = resolver
        self.task_root = self.root / str(actor_id) / str(task.id)
        self.manifest_path = self.task_root / "sources.json"
        self.state = self._load()

    def reload(self) -> LocalSourceState:
        self.state = self._load()
        return self.state

    def add_paths(
        self,
        role: str,
        paths: Iterable[Path],
    ) -> LocalSourceState:
        if role not in ("garment", "model"):
            raise ValueError("不支持的素材类型")
        garments = list(self.state.garments)
        model = self.state.model
        known = {image.content_hash for image in garments}
        for raw_path in paths:
            source = Path(raw_path).expanduser().resolve()
            if (
                not source.is_file()
                or source.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES
            ):
                continue
            data = source.read_bytes()
            mime, width, height = image_metadata(data)
            content_hash = sha256_bytes(data)
            if role == "garment" and content_hash in known:
                continue
            if role == "garment" and len(garments) >= 13:
                raise ValueError("衣服图最多 13 张")
            stored = self._copy_image(
                source,
                role,
                original_name=source.name,
                source_path=source,
                content_hash=content_hash,
                mime=mime,
                width=width,
                height=height,
            )
            if role == "model":
                model = stored
            else:
                garments.append(stored)
                known.add(content_hash)
        if len(garments) > 13:
            raise ValueError("衣服图最多 13 张")
        self.state = LocalSourceState(
            self.task.source_revision,
            tuple(garments),
            model,
        )
        self._save()
        return self.state

    def replace_image(
        self,
        role: str,
        index: int,
        image: StoredImage,
    ) -> LocalSourceState:
        stored = self._copy_image(
            image.path,
            role,
            original_name=image.original_name,
            source_path=image.source_path,
            content_hash=image.content_hash,
            mime=image.mime,
            width=image.width,
            height=image.height,
        )
        if role == "garment":
            garments = list(self.state.garments)
            if not 0 <= index < len(garments):
                raise ValueError("衣服图选择无效")
            garments[index] = stored
            self.state = LocalSourceState(
                self.state.source_revision,
                tuple(garments),
                self.state.model,
            )
        elif role == "model" and index == 0:
            self.state = LocalSourceState(
                self.state.source_revision,
                self.state.garments,
                stored,
            )
        else:
            raise ValueError("模特图选择无效")
        self._save()
        return self.state

    def replace_all(
        self,
        garments: Iterable[StoredImage],
        model: StoredImage | None,
    ) -> LocalSourceState:
        copied_garments = tuple(
            self._copy_image(
                image.path,
                "garment",
                original_name=image.original_name,
                source_path=image.source_path or image.path,
                content_hash=image.content_hash,
                mime=image.mime,
                width=image.width,
                height=image.height,
            )
            for image in garments
        )
        copied_model = (
            self._copy_image(
                model.path,
                "model",
                original_name=model.original_name,
                source_path=model.source_path or model.path,
                content_hash=model.content_hash,
                mime=model.mime,
                width=model.width,
                height=model.height,
            )
            if model is not None
            else None
        )
        self.state = LocalSourceState(
            self.task.source_revision,
            copied_garments,
            copied_model,
        )
        self._save()
        return self.state

    def restore_image(self, role: str, index: int) -> LocalSourceState:
        current = self.image_at(role, index)
        if current is None or current.source_path is None:
            raise ValueError("这张图片没有可恢复的原图")
        restored = self._copy_image(
            current.source_path,
            role,
            original_name=current.source_path.name,
            source_path=current.source_path,
            content_hash=current.content_hash,
            mime=current.mime,
            width=current.width,
            height=current.height,
        )
        if role == "garment":
            garments = list(self.state.garments)
            garments[index] = restored
            self.state = LocalSourceState(
                self.state.source_revision,
                tuple(garments),
                self.state.model,
            )
        else:
            self.state = LocalSourceState(
                self.state.source_revision,
                self.state.garments,
                restored,
            )
        self._save()
        return self.state

    def remove_images(
        self,
        role: str,
        indexes: Iterable[int],
    ) -> LocalSourceState:
        selected = set(int(index) for index in indexes)
        if role == "garment":
            garments = tuple(
                image
                for index, image in enumerate(self.state.garments)
                if index not in selected
            )
            self.state = LocalSourceState(
                self.state.source_revision,
                garments,
                self.state.model,
            )
        elif role == "model" and 0 in selected:
            self.state = LocalSourceState(
                self.state.source_revision,
                self.state.garments,
                None,
            )
        else:
            raise ValueError("模特图选择无效")
        self._save()
        return self.state

    def clear(self) -> LocalSourceState:
        self.state = LocalSourceState(
            self.task.source_revision,
            (),
            None,
        )
        self._save()
        return self.state

    def restore_all(self) -> LocalSourceState:
        self.state = self._seed_from_task()
        return self.state

    def save_attempt_snapshot(
        self,
        attempt_id: int,
    ) -> None:
        path = self._attempt_snapshot_path(attempt_id)
        files_folder = (
            self.task_root
            / "attempt_sources"
            / f"{int(attempt_id)}_files"
        )
        files_folder.mkdir(parents=True, exist_ok=True)
        payload = []
        for image in self.state.references():
            suffix = image.path.suffix.lower() or ".png"
            destination = files_folder / (
                f"{image.content_hash[:20]}{suffix}"
            )
            if not destination.exists():
                atomic_copy_file(image.path, destination)
            payload.append(
                self._serialize_image(
                    StoredImage(
                        path=destination,
                        content_hash=image.content_hash,
                        mime=image.mime,
                        width=image.width,
                        height=image.height,
                        original_name=image.original_name,
                        source_path=image.source_path,
                    )
                )
            )
        atomic_write_bytes(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def load_attempt_snapshot(
        self,
        attempt_id: int,
    ) -> tuple[StoredImage, ...] | None:
        path = self._attempt_snapshot_path(attempt_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text("utf-8"))
            images = tuple(
                self._deserialize_image(item)
                for item in payload
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        if not images or not all(image.path.is_file() for image in images):
            return None
        return images

    def image_at(self, role: str, index: int) -> StoredImage | None:
        if role == "garment":
            return (
                self.state.garments[index]
                if 0 <= index < len(self.state.garments)
                else None
            )
        return self.state.model if role == "model" and index == 0 else None

    def _load(self) -> LocalSourceState:
        if self.manifest_path.is_file():
            try:
                data = json.loads(self.manifest_path.read_text("utf-8"))
                if int(data.get("source_revision", 0)) == int(
                    self.task.source_revision
                ):
                    garments = tuple(
                        self._deserialize_image(item)
                        for item in data.get("garments", [])
                    )
                    model_data = data.get("model")
                    model = (
                        self._deserialize_image(model_data)
                        if model_data
                        else None
                    )
                    if all(image.path.is_file() for image in garments):
                        if model is None or model.path.is_file():
                            return LocalSourceState(
                                self.task.source_revision,
                                garments,
                                model,
                            )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        return self._seed_from_task()

    def _seed_from_task(self) -> LocalSourceState:
        self.task_root.mkdir(parents=True, exist_ok=True)
        for folder_name in ("garments", "model"):
            folder = self.task_root / folder_name
            if folder.exists():
                shutil.rmtree(folder)
        self.manifest_path.unlink(missing_ok=True)
        garments = []
        for asset in self.task.garment_assets:
            source = self.resolver.resolve_project_path(
                self.task.project_relative_root,
                asset.relative_path,
            )
            garments.append(
                self._copy_image(
                    source,
                    "garment",
                    original_name=asset.original_name,
                    source_path=source,
                    content_hash=asset.content_hash,
                    mime=asset.mime,
                    width=asset.width,
                    height=asset.height,
                )
            )
        model_path = self.resolver.resolve_project_path(
            self.task.project_relative_root,
            self.task.model_path,
        )
        model = self._copy_image(
            model_path,
            "model",
            original_name=self.task.model_name,
            source_path=model_path,
            content_hash=self.task.model_hash,
            mime="image/png",
            width=0,
            height=0,
        )
        state = LocalSourceState(
            self.task.source_revision,
            tuple(garments),
            model,
        )
        self.state = state
        self._save()
        return state

    def _copy_image(
        self,
        source: Path,
        role: str,
        *,
        original_name: str,
        source_path: Path | None,
        content_hash: str,
        mime: str,
        width: int,
        height: int,
    ) -> StoredImage:
        source = Path(source).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"参考图不存在：{source}")
        data = source.read_bytes()
        actual_hash = sha256_bytes(data)
        actual_mime, actual_width, actual_height = image_metadata(data)
        folder = self.task_root / ("garments" if role == "garment" else "model")
        folder.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower() or ".png"
        destination = folder / f"{actual_hash[:20]}{suffix}"
        if not destination.exists():
            atomic_copy_file(source, destination)
        return StoredImage(
            path=destination,
            content_hash=actual_hash,
            mime=actual_mime or mime,
            width=actual_width or width,
            height=actual_height or height,
            original_name=original_name,
            source_path=Path(source_path).resolve() if source_path else source,
        )

    def _save(self) -> None:
        self.task_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": self.task.id,
            "source_revision": self.state.source_revision,
            "garments": [
                self._serialize_image(image)
                for image in self.state.garments
            ],
            "model": (
                self._serialize_image(self.state.model)
                if self.state.model is not None
                else None
            ),
        }
        atomic_write_bytes(
            self.manifest_path,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8"),
        )

    def _attempt_snapshot_path(self, attempt_id: int) -> Path:
        folder = self.task_root / "attempt_sources"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{int(attempt_id)}.json"

    def _serialize_image(self, image: StoredImage) -> dict:
        return {
            "path": image.path.relative_to(self.task_root).as_posix(),
            "content_hash": image.content_hash,
            "mime": image.mime,
            "width": image.width,
            "height": image.height,
            "original_name": image.original_name,
            "source_path": (
                str(image.source_path)
                if image.source_path is not None
                else ""
            ),
        }

    def _deserialize_image(self, data: dict) -> StoredImage:
        path = (self.task_root / str(data["path"])).resolve()
        path.relative_to(self.task_root)
        source_text = str(data.get("source_path") or "")
        return StoredImage(
            path=path,
            content_hash=str(data["content_hash"]),
            mime=str(data["mime"]),
            width=int(data["width"]),
            height=int(data["height"]),
            original_name=str(data["original_name"]),
            source_path=Path(source_text) if source_text else None,
        )
