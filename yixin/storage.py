from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps

from .models import StoredImage
from .paths import AppPaths


FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}
MIME_TO_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}
SUPPORTED_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
)


class ImageValidationError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_copy_file(source: Path, destination: Path) -> None:
    data = source.read_bytes()
    atomic_write_bytes(destination, data)


def image_metadata(data: bytes) -> tuple[str, int, int]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image_format = (image.format or "").upper()
            width, height = image.size
    except Exception as exc:
        raise ImageValidationError("文件不是可识别的图片") from exc
    mime = FORMAT_TO_MIME.get(image_format)
    if not mime:
        raise ImageValidationError(f"暂不支持 {image_format or '未知'} 图片格式")
    return mime, width, height


def data_url_for_file(path: Path, mime: str | None = None) -> str:
    data = path.read_bytes()
    if mime is None:
        mime, _, _ = image_metadata(data)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class FileStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def save_reference(self, source: Path) -> StoredImage:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise ImageValidationError("参考图文件不存在")
        data = source.read_bytes()
        content_hash = sha256_bytes(data)
        mime, width, height = image_metadata(data)
        suffix = MIME_TO_SUFFIX[mime]
        destination = (
            self.paths.temp
            / "inputs"
            / content_hash[:2]
            / f"{content_hash}{suffix}"
        )
        if not destination.exists():
            atomic_write_bytes(destination, data)
        else:
            # 复用旧文件时刷新时间戳，避免被临时文件清理当成过期文件删除。
            destination.touch()
        return StoredImage(
            path=destination,
            content_hash=content_hash,
            mime=mime,
            width=width,
            height=height,
            original_name=source.name,
            source_path=source,
        )

    def save_cropped_reference(
        self,
        reference: StoredImage,
        crop_box: tuple[int, int, int, int],
    ) -> StoredImage:
        with Image.open(reference.path) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
            width, height = image.size
            left, top, right, bottom = crop_box
            left = max(0, min(left, width - 1))
            top = max(0, min(top, height - 1))
            right = max(left + 1, min(right, width))
            bottom = max(top + 1, min(bottom, height))
            cropped = image.crop((left, top, right, bottom))
            buffer = io.BytesIO()
            cropped.save(buffer, format="PNG", optimize=True)
            data = buffer.getvalue()
        content_hash = sha256_bytes(data)
        destination = (
            self.paths.temp
            / "inputs"
            / "crops"
            / content_hash[:2]
            / f"{content_hash}.png"
        )
        if not destination.exists():
            atomic_write_bytes(destination, data)
        source_path = reference.source_path or reference.path
        return StoredImage(
            path=destination,
            content_hash=content_hash,
            mime="image/png",
            width=right - left,
            height=bottom - top,
            original_name=f"{Path(source_path).stem}_crop.png",
            source_path=source_path,
        )

    def store_generated(
        self,
        task_id: int,
        data: bytes,
        index: int,
    ) -> StoredImage:
        mime, width, height = image_metadata(data)
        suffix = MIME_TO_SUFFIX.get(mime)
        if suffix is None:
            mime = "image/png"
            suffix = ".png"
        content_hash = sha256_bytes(data)
        destination = (
            self.paths.temp
            / f"task_{task_id}"
            / f"{index + 1:02d}_{content_hash[:16]}{suffix}"
        )
        atomic_write_bytes(destination, data)
        return StoredImage(
            path=destination,
            content_hash=content_hash,
            mime=mime,
            width=width,
            height=height,
            original_name=destination.name,
        )

    def create_thumbnail(
        self,
        image_path: Path,
        destination: Path | None = None,
        size: tuple[int, int] = (360, 360),
    ) -> Path:
        if destination is None:
            destination = image_path.with_name(f"{image_path.stem}_thumb.jpg")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.", suffix=".jpg", dir=destination.parent
        )
        os.close(descriptor)
        try:
            with Image.open(image_path) as source:
                image = ImageOps.exif_transpose(source)
                if image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                image.thumbnail(size, Image.Resampling.LANCZOS)
                image.save(temp_name, format="JPEG", quality=88, optimize=True)
            os.replace(temp_name, destination)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
        return destination

    def copy_to_library(self, folder: Path, source: Path, filename: str) -> Path:
        if not self.paths.is_under(folder, self.paths.library):
            raise ValueError("作品目录必须位于 library 内")
        suffix = source.suffix or ".png"
        destination = folder / f"{filename}{suffix}"
        atomic_copy_file(source, destination)
        return destination

    def library_folder(self, folder_name: str) -> Path:
        folder = self.paths.library / folder_name
        if not self.paths.is_under(folder, self.paths.library):
            raise ValueError("无效的作品目录")
        return folder

    def remove_work_folder(self, folder: Path) -> None:
        resolved = folder.resolve()
        if not self.paths.is_under(resolved, self.paths.library) or resolved == self.paths.library:
            raise ValueError("拒绝删除 library 目录以外的内容")
        if resolved.exists():
            shutil.rmtree(resolved)

    def cleanup_temp(
        self,
        protected_paths: Iterable[Path],
        *,
        days: int = 7,
        now: datetime | None = None,
    ) -> list[Path]:
        cutoff = (now or utc_now()) - timedelta(days=days)
        protected = {path.resolve() for path in protected_paths}
        removed: list[Path] = []
        temp_root = self.paths.temp.resolve()
        if not temp_root.exists():
            return removed

        for path in sorted(temp_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in protected:
                continue
            if path.is_file():
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if modified < cutoff:
                    path.unlink(missing_ok=True)
                    removed.append(path)
            elif path.is_dir():
                try:
                    if not any(path.iterdir()):
                        path.rmdir()
                except OSError:
                    pass
        return removed
