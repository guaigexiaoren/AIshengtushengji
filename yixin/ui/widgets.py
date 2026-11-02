from __future__ import annotations

import os
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImageReader,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QListWidget,
    QListWidgetItem,
    QWidget,
)

from ..models import StoredImage
from ..storage import SUPPORTED_IMAGE_SUFFIXES


SUPPORTED_REFERENCE_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES
PREVIEW_CANVAS_COLOR = "#eef2f7"
PREVIEW_TEXT_COLOR = "#60708a"

_THUMBNAIL_CACHE: OrderedDict[tuple[str, int, int, int], QPixmap] = OrderedDict()
_THUMBNAIL_CACHE_LIMIT = 192


def natural_sort_key(relative_path: str) -> tuple[tuple[int, str | int], ...]:
    parts = re.split(r"(\d+)", relative_path.replace("\\", "/").casefold())
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in parts
        if part
    )


def scan_image_inputs(inputs: Sequence[Path]) -> tuple[list[Path], list[str]]:
    images: list[Path] = []
    errors: list[str] = []

    for raw_path in inputs:
        path = Path(raw_path).expanduser()
        if path.is_file():
            if path.suffix.lower() in SUPPORTED_REFERENCE_SUFFIXES:
                images.append(path.resolve())
            continue
        if not path.is_dir():
            errors.append(f"{path}：路径不存在")
            continue

        folder_images: list[tuple[str, Path]] = []

        def on_error(error: OSError) -> None:
            errors.append(f"{error.filename or path}：{error.strerror or error}")

        for root, directories, filenames in os.walk(
            path,
            followlinks=False,
            onerror=on_error,
        ):
            root_path = Path(root)
            directories[:] = [
                name
                for name in directories
                if not (root_path / name).is_symlink()
            ]
            for filename in filenames:
                candidate = root_path / filename
                if candidate.suffix.lower() not in SUPPORTED_REFERENCE_SUFFIXES:
                    continue
                folder_images.append(
                    (candidate.relative_to(path).as_posix(), candidate.resolve())
                )
        images.extend(
            candidate
            for _, candidate in sorted(
                folder_images,
                key=lambda item: natural_sort_key(item[0]),
            )
        )
    return images, errors


def load_scaled_pixmap(
    path: str | Path,
    target_size: QSize,
    *,
    cache: bool = True,
) -> QPixmap:
    source = Path(path)
    target = QSize(max(1, target_size.width()), max(1, target_size.height()))
    try:
        modified = source.stat().st_mtime_ns
    except OSError:
        return QPixmap()
    key = (str(source.resolve()), modified, target.width(), target.height())
    if cache and key in _THUMBNAIL_CACHE:
        pixmap = _THUMBNAIL_CACHE.pop(key)
        _THUMBNAIL_CACHE[key] = pixmap
        return pixmap
    reader = QImageReader(str(source))
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid():
        scaled_size = source_size.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio
        )
        reader.setScaledSize(scaled_size)
    image = reader.read()
    if image.isNull():
        return QPixmap()
    pixmap = QPixmap.fromImage(image)
    if pixmap.width() > target.width() or pixmap.height() > target.height():
        pixmap = pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    if cache:
        _THUMBNAIL_CACHE[key] = pixmap
        while len(_THUMBNAIL_CACHE) > _THUMBNAIL_CACHE_LIMIT:
            _THUMBNAIL_CACHE.popitem(last=False)
    return pixmap


@dataclass(frozen=True)
class TaskImageAsset:
    role: str
    position: int
    label: str
    path: Path
    thumbnail_path: Path
    payload: StoredImage | Mapping

    @property
    def is_result(self) -> bool:
        return self.role == "result"

    @property
    def is_reference(self) -> bool:
        return self.role == "reference"


def build_task_assets(
    references: Sequence[StoredImage],
    results: Sequence[Mapping],
) -> list[TaskImageAsset]:
    assets: list[TaskImageAsset] = []
    for index, reference in enumerate(references):
        assets.append(
            TaskImageAsset(
                role="reference",
                position=index,
                label=f"参考 {index + 1}",
                path=Path(reference.path),
                thumbnail_path=Path(reference.path),
                payload=reference,
            )
        )
    for index, result in enumerate(results):
        assets.append(
            TaskImageAsset(
                role="result",
                position=index,
                label=f"结果 {index + 1}",
                path=Path(result["path"]),
                thumbnail_path=Path(result["thumbnail_path"] or result["path"]),
                payload=result,
            )
        )
    return assets


def local_image_paths(urls: Sequence[QUrl]) -> list[Path]:
    paths: list[Path] = []
    for url in urls:
        if not url.isLocalFile():
            continue
        path = Path(url.toLocalFile())
        if path.is_file() and path.suffix.lower() in SUPPORTED_REFERENCE_SUFFIXES:
            paths.append(path)
    return paths


class ImageView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._display_size = QSize()
        self.current_path: Path | None = None
        self._placeholder = "生成结果会显示在这里"
        self.setMinimumSize(320, 280)

    def set_image(self, path: str | Path | None) -> None:
        if path:
            self.current_path = Path(path)
        else:
            self.current_path = None
        self._display_size = QSize()
        self._refresh_image()
        self.update()

    def _refresh_image(self) -> None:
        available = self.rect().adjusted(12, 12, -12, -12)
        target = available.size()
        if not self.current_path or target.width() <= 0 or target.height() <= 0:
            self._pixmap = None
            return
        pixmap = load_scaled_pixmap(
            self.current_path, target, cache=False
        )
        self._pixmap = pixmap if not pixmap.isNull() else None
        self._display_size = target

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        target = self.rect().adjusted(12, 12, -12, -12).size()
        if target != self._display_size:
            self._refresh_image()
            self.update()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(PREVIEW_CANVAS_COLOR))
        if not self._pixmap:
            painter.setPen(QColor(PREVIEW_TEXT_COLOR))
            font = QFont(self.font())
            font.setPointSize(12)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
            return
        available = QRectF(self.rect()).adjusted(12, 12, -12, -12)
        target = QRectF(self._pixmap.rect())
        target.moveCenter(available.center())
        painter.drawPixmap(target.toRect().topLeft(), self._pixmap)


class CompareWidget(QWidget):
    split_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._left = QPixmap()
        self._right = QPixmap()
        self._left_path: Path | None = None
        self._right_path: Path | None = None
        self._loaded_size = QSize()
        self._split = 0.5
        self._dragging = False
        self.setMinimumSize(480, 320)
        self.setMouseTracking(True)

    def set_images(self, left: str | Path, right: str | Path) -> None:
        self._left_path = Path(left)
        self._right_path = Path(right)
        self._loaded_size = QSize()
        self._refresh_images()
        self.update()

    def _refresh_images(self) -> None:
        target = self.rect().adjusted(10, 10, -10, -10).size()
        if target.width() <= 0 or target.height() <= 0:
            self._left = QPixmap()
            self._right = QPixmap()
            return
        self._left = (
            load_scaled_pixmap(self._left_path, target, cache=False)
            if self._left_path
            else QPixmap()
        )
        self._right = (
            load_scaled_pixmap(self._right_path, target, cache=False)
            if self._right_path
            else QPixmap()
        )
        self._loaded_size = target

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        target = self.rect().adjusted(10, 10, -10, -10).size()
        if target != self._loaded_size:
            self._refresh_images()
            self.update()

    def set_split(self, value: float) -> None:
        self._split = max(0.02, min(0.98, value))
        self.update()
        self.split_changed.emit(self._split)

    def _image_rect(self) -> QRectF:
        available = QRectF(self.rect()).adjusted(10, 10, -10, -10)
        base = next(
            (pixmap for pixmap in (self._left, self._right) if not pixmap.isNull()),
            None,
        )
        if base is None:
            return available
        source_size = base.size()
        ratio = min(
            available.width() / max(1, source_size.width()),
            available.height() / max(1, source_size.height()),
        )
        target = QRectF(
            0,
            0,
            source_size.width() * ratio,
            source_size.height() * ratio,
        )
        target.moveCenter(available.center())
        return target

    @staticmethod
    def _pixmap_rect(pixmap: QPixmap, target: QRectF) -> QRectF:
        if pixmap.isNull() or target.width() <= 0 or target.height() <= 0:
            return target
        rect = QRectF(pixmap.rect())
        rect.moveCenter(target.center())
        return rect

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#171d29"))
        target = self._image_rect()
        if self._right.isNull() and self._left.isNull():
            painter.setPen(QColor("#8492a8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "选择两张图片进行对比")
            return
        if not self._right.isNull():
            right_rect = self._pixmap_rect(self._right, target)
            painter.drawPixmap(right_rect.toRect().topLeft(), self._right)
        if not self._left.isNull():
            left_rect = self._pixmap_rect(self._left, target)
            split_x = target.left() + target.width() * self._split
            left_clip = QRectF(
                target.left(), target.top(), split_x - target.left(), target.height()
            )
            painter.save()
            painter.setClipRect(left_clip)
            painter.drawPixmap(left_rect.toRect().topLeft(), self._left)
            painter.restore()
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(QPointF(split_x, target.top()), QPointF(split_x, target.bottom()))
            painter.setBrush(QColor("#3f73cb"))
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawEllipse(QPointF(split_x, target.center().y()), 12, 12)
            painter.setPen(QColor("#ffffff"))
            painter.setFont(QFont(self.font().family(), 9, QFont.Weight.DemiBold))
            painter.drawText(
                QRectF(target.left() + 8, target.top() + 8, 100, 24),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                "左侧",
            )
            painter.drawText(
                QRectF(target.right() - 108, target.top() + 8, 100, 24),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                "右侧",
            )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._update_split(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._update_split(event.position().x())

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = False

    def _update_split(self, x_position: float) -> None:
        target = self._image_rect()
        if target.width() <= 0:
            return
        self.set_split((x_position - target.left()) / target.width())


class ImageStrip(QListWidget):
    def __init__(self, parent: QWidget | None = None, *, icon_size: int = 84) -> None:
        super().__init__(parent)
        self.setObjectName("ImageStrip")
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setWrapping(False)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setGridSize(QSize(icon_size + 18, icon_size + 34))
        self.setFixedHeight(icon_size + 54)
        self._thumbnail_contexts: dict[int, QListWidgetItem] = {}
        self._thumbnail_loader = None

    def set_images(self, items: list[tuple[str | Path, str, object]]) -> None:
        self.clear()
        for path, label, data in items:
            item = QListWidgetItem(label)
            pixmap = load_scaled_pixmap(path, self.iconSize())
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            item.setData(Qt.ItemDataRole.UserRole, data)
            item.setToolTip(str(path))
            self.addItem(item)
        if self.count():
            self.setCurrentRow(0)

    def set_images_async(
        self,
        items: list[tuple[str | Path, str, object]],
    ) -> None:
        if self._thumbnail_loader is None:
            from .async_thumbnails import get_thumbnail_loader

            self._thumbnail_loader = get_thumbnail_loader()
            self._thumbnail_loader.thumbnail_ready.connect(
                self._thumbnail_ready
            )
            self._thumbnail_loader.thumbnail_failed.connect(
                self._thumbnail_failed
            )
        self.clear()
        self._thumbnail_contexts.clear()
        for path, label, data in items:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, data)
            item.setToolTip(str(path))
            self.addItem(item)
            request = self._thumbnail_loader.request_thumbnail(
                path,
                self.iconSize(),
            )
            self._thumbnail_contexts[request.request_id] = item
        if self.count():
            self.setCurrentRow(0)

    def _thumbnail_ready(
        self,
        request_id: int,
        _path: str,
        image: QImage,
    ) -> None:
        item = self._thumbnail_contexts.pop(request_id, None)
        if item is None:
            return
        pixmap = QPixmap.fromImage(
            image.scaled(
                self.iconSize(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        if not pixmap.isNull():
            item.setIcon(QIcon(pixmap))

    def _thumbnail_failed(
        self,
        request_id: int,
        _path: str,
        _message: str,
    ) -> None:
        self._thumbnail_contexts.pop(request_id, None)


class ReorderableImageStrip(ImageStrip):
    order_changed = Signal(list)
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None, *, icon_size: int = 84) -> None:
        super().__init__(parent, icon_size=icon_size)
        self.setDragEnabled(False)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        self.setMouseTracking(True)
        self._drop_insert_row: int | None = None
        self._drag_source_row: int | None = None
        self._drag_start_pos = QPoint()
        self._drag_active = False

    def _emit_order_changed(self) -> None:
        self.order_changed.emit(
            [
                self.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(self.count())
            ]
        )

    def move_item(self, source_row: int, target_row: int) -> None:
        if not 0 <= source_row < self.count():
            return
        target_row = max(0, min(target_row, self.count() - 1))
        if source_row == target_row:
            return
        item = self.takeItem(source_row)
        self.insertItem(target_row, item)
        self.setCurrentRow(target_row)
        self._emit_order_changed()

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            self._drag_source_row = index.row()
            self._drag_start_pos = event.position().toPoint()
            self._drag_active = False
        else:
            self._reset_drag()

    def mouseMoveEvent(self, event) -> None:
        position = event.position().toPoint()
        if self._drag_source_row is not None:
            if (
                not self._drag_active
                and (position - self._drag_start_pos).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                self._drag_active = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._drag_active:
                self._auto_scroll(position)
                self._drop_insert_row = self._insert_row_at(position)
                self.viewport().update()
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_active:
            source_row = self._drag_source_row
            position = event.position().toPoint()
            inside = self.viewport().rect().contains(position)
            insertion_row = self._insert_row_at(position) if inside else None
            self._reset_drag()
            if source_row is not None and insertion_row is not None:
                target_row = (
                    insertion_row - 1
                    if insertion_row > source_row
                    else insertion_row
                )
                target_row = max(0, min(target_row, self.count() - 1))
                self.move_item(source_row, target_row)
            event.accept()
            return
        self._reset_drag()
        super().mouseReleaseEvent(event)

    def _reset_drag(self) -> None:
        self._drag_source_row = None
        self._drag_start_pos = QPoint()
        self._drag_active = False
        self._drop_insert_row = None
        self.unsetCursor()
        self.viewport().update()

    def _auto_scroll(self, position: QPoint) -> None:
        margin = 24
        scrollbar = self.horizontalScrollBar()
        if position.x() < margin:
            scrollbar.setValue(scrollbar.value() - max(2, (margin - position.x()) // 2))
        elif position.x() > self.viewport().width() - margin:
            scrollbar.setValue(
                scrollbar.value()
                + max(2, (position.x() - self.viewport().width() + margin) // 2)
            )

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            if local_image_paths(event.mimeData().urls()):
                self.setStyleSheet(
                    "QListWidget#ImageStrip { border: 2px solid #3f73cb;"
                    " background: #edf4ff; }"
                )
                event.acceptProposedAction()
            else:
                event.ignore()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasUrls():
            if local_image_paths(event.mimeData().urls()):
                event.acceptProposedAction()
            else:
                event.ignore()
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            self.setStyleSheet("")
        if self._drag_active:
            self._drop_insert_row = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            paths = local_image_paths(event.mimeData().urls())
            self.setStyleSheet("")
            if not paths:
                event.ignore()
                return
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        event.ignore()

    def _insert_row_at(self, position: QPoint) -> int:
        for row in range(self.count()):
            rect = self.visualItemRect(self.item(row))
            if position.x() < rect.center().x():
                return row
        return self.count()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._drop_insert_row is None:
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#3f73cb"), 4))
        if self.count() == 0:
            x = 6
        elif self._drop_insert_row <= 0:
            x = max(3, self.visualItemRect(self.item(0)).left() - 5)
        elif self._drop_insert_row >= self.count():
            x = min(
                self.viewport().width() - 4,
                self.visualItemRect(self.item(self.count() - 1)).right() + 5,
            )
        else:
            x = self.visualItemRect(self.item(self._drop_insert_row)).left() - 5
        painter.drawLine(x, 8, x, self.viewport().height() - 8)


class ReferenceDropGroup(QGroupBox):
    files_dropped = Signal(list)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.setAcceptDrops(True)
        self._normal_style = ""

    def dragEnterEvent(self, event) -> None:
        paths = local_image_paths(event.mimeData().urls())
        if paths:
            self.setStyleSheet(
                "QGroupBox { border: 2px solid #3f73cb; border-radius: 10px;"
                " background: #edf4ff; }"
            )
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if local_image_paths(event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet(self._normal_style)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        paths = local_image_paths(event.mimeData().urls())
        self.setStyleSheet(self._normal_style)
        if not paths:
            event.ignore()
            return
        self.files_dropped.emit(paths)
        event.acceptProposedAction()
