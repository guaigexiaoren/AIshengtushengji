from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QRunnable,
    QSize,
    QThreadPool,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QImage, QImageReader


THUMBNAIL_BASE_SIZE = 128
THUMBNAIL_CACHE_LIMIT = 512
THUMBNAIL_THREAD_COUNT = 4


@dataclass(frozen=True)
class ThumbnailRequest:
    request_id: int
    path: Path
    target_size: QSize


class _ThumbnailTaskSignals(QObject):
    loaded = Signal(object, object)
    failed = Signal(object, str)


class _ThumbnailTask(QRunnable):
    def __init__(self, path: Path, cache_key: tuple[str, int, int]) -> None:
        super().__init__()
        self.path = path
        self.cache_key = cache_key
        self.signals = _ThumbnailTaskSignals()

    def run(self) -> None:
        reader = QImageReader(str(self.path))
        reader.setAutoTransform(True)
        source_size = reader.size()
        if source_size.isValid():
            reader.setScaledSize(
                source_size.scaled(
                    THUMBNAIL_BASE_SIZE,
                    THUMBNAIL_BASE_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                )
            )
        image = reader.read()
        if image.isNull():
            self.signals.failed.emit(
                self.cache_key,
                reader.errorString() or "无法读取图片",
            )
            return
        self.signals.loaded.emit(self.cache_key, image)


class ThumbnailLoader(QObject):
    thumbnail_ready = Signal(int, str, object)
    thumbnail_failed = Signal(int, str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(THUMBNAIL_THREAD_COUNT)
        self._cache: OrderedDict[tuple[str, int, int], QImage] = OrderedDict()
        self._requests: dict[int, QSize] = {}
        self._inflight: dict[tuple[str, int, int], list[int]] = {}
        self._next_request_id = 1

    def request_thumbnail(
        self,
        path: str | Path,
        target_size: QSize,
    ) -> ThumbnailRequest:
        source = Path(path)
        try:
            stat = source.stat()
            source_key = str(source.resolve())
            key = (source_key, stat.st_mtime_ns, stat.st_size)
        except OSError:
            source_key = str(source)
            key = (source_key, -1, -1)
        request_id = self._next_request_id
        self._next_request_id += 1
        target = QSize(
            max(1, target_size.width()),
            max(1, target_size.height()),
        )
        self._requests[request_id] = target
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            QTimer.singleShot(
                0,
                lambda: self._emit_ready(request_id, key, cached),
            )
            return ThumbnailRequest(request_id, source, target)

        waiting = self._inflight.get(key)
        if waiting is not None:
            waiting.append(request_id)
            return ThumbnailRequest(request_id, source, target)

        self._inflight[key] = [request_id]
        task = _ThumbnailTask(source, key)
        task.signals.loaded.connect(self._loaded)
        task.signals.failed.connect(self._failed)
        self._pool.start(task)
        return ThumbnailRequest(request_id, source, target)

    def invalidate(self, path: str | Path) -> None:
        source = str(Path(path).resolve())
        for key in tuple(self._cache):
            if key[0] == source:
                self._cache.pop(key, None)

    def clear_cache(self) -> None:
        self._cache.clear()

    def cache_size(self) -> int:
        return len(self._cache)

    def _loaded(
        self,
        key: tuple[str, int, int],
        image: QImage,
    ) -> None:
        self._cache[key] = image
        self._cache.move_to_end(key)
        while len(self._cache) > THUMBNAIL_CACHE_LIMIT:
            self._cache.popitem(last=False)
        for request_id in self._inflight.pop(key, []):
            self._emit_ready(request_id, key, image)

    def _failed(
        self,
        key: tuple[str, int, int],
        message: str,
    ) -> None:
        for request_id in self._inflight.pop(key, []):
            if self._requests.pop(request_id, None) is None:
                continue
            self.thumbnail_failed.emit(request_id, key[0], message)

    def _emit_ready(
        self,
        request_id: int,
        key: tuple[str, int, int],
        image: QImage,
    ) -> None:
        if self._requests.pop(request_id, None) is None:
            return
        self.thumbnail_ready.emit(request_id, key[0], image)


_thumbnail_loader: ThumbnailLoader | None = None


def get_thumbnail_loader() -> ThumbnailLoader:
    global _thumbnail_loader
    if _thumbnail_loader is None:
        _thumbnail_loader = ThumbnailLoader(
            QCoreApplication.instance()
        )
    return _thumbnail_loader
