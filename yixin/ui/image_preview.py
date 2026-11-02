from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QImageReader, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.shared_paths import SharedPathResolver
from .widgets import ImageStrip, ImageView


class ClickableImageView(ImageView):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.current_path is not None
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ZoomableImageView(QGraphicsView):
    scale_changed = Signal(float)

    MIN_SCALE = 0.1
    MAX_SCALE = 4.0

    def __init__(
        self,
        scene: QGraphicsScene,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(scene, parent)
        self._scale = 1.0
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorViewCenter
        )
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor("#171d29"))
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    @property
    def scale_factor(self) -> float:
        return self._scale

    def set_pixmap_item(self, item: QGraphicsPixmapItem) -> None:
        self._pixmap_item = item

    def clear_pixmap_item(self) -> None:
        self._pixmap_item = None

    def zoom_by(self, factor: float) -> None:
        self.set_scale(self._scale * factor)

    def set_scale(
        self,
        value: float,
        *,
        anchor: QGraphicsView.ViewportAnchor | None = None,
    ) -> None:
        target = max(self.MIN_SCALE, min(self.MAX_SCALE, value))
        if abs(target - self._scale) < 0.0001:
            return
        previous_anchor = self.transformationAnchor()
        if anchor is not None:
            self.setTransformationAnchor(anchor)
        ratio = target / self._scale
        self.scale(ratio, ratio)
        if anchor is not None:
            self.setTransformationAnchor(previous_anchor)
        self._scale = target
        self.scale_changed.emit(target)

    def fit_to_window(self) -> None:
        self.resetTransform()
        if self._pixmap_item is not None:
            self.fitInView(
                self._pixmap_item,
                Qt.AspectRatioMode.KeepAspectRatio,
            )
            fitted = self.transform().m11()
            target = max(self.MIN_SCALE, min(self.MAX_SCALE, fitted))
            if abs(target - fitted) > 0.0001:
                self.setTransform(QTransform().scale(target, target))
            self.centerOn(self._pixmap_item)
        else:
            target = 1.0
        self._scale = target
        self.scale_changed.emit(target)

    def wheelEvent(self, event) -> None:
        if self._pixmap_item is None:
            super().wheelEvent(event)
            return
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.set_scale(
            self._scale * factor,
            anchor=QGraphicsView.ViewportAnchor.AnchorUnderMouse,
        )
        event.accept()

    def mousePressEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._pixmap_item is not None
        ):
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)


class LargeImageDialog(QDialog):
    def __init__(self, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("查看大图")
        self.resize(1100, 800)
        self._pixmap = QPixmap(str(path))
        self._scale = 1.0
        self._fit_pending = True
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.zoom_in_button = QPushButton("放大")
        self.zoom_out_button = QPushButton("缩小")
        self.fit_button = QPushButton("适应窗口")
        self.actual_button = QPushButton("100%")
        self.scale_label = QLabel("100%")
        toolbar.addWidget(self.zoom_in_button)
        toolbar.addWidget(self.zoom_out_button)
        toolbar.addWidget(self.fit_button)
        toolbar.addWidget(self.actual_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.scale_label)
        layout.addLayout(toolbar)

        self.scene = QGraphicsScene(self)
        self.pixmap_item = QGraphicsPixmapItem(self._pixmap)
        self.scene.addItem(self.pixmap_item)
        self.view = ZoomableImageView(self.scene)
        self.view.set_pixmap_item(self.pixmap_item)
        self.view.scale_changed.connect(self._scale_changed)
        layout.addWidget(self.view, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.accept)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self.zoom_in_button.clicked.connect(lambda: self.view.zoom_by(1.2))
        self.zoom_out_button.clicked.connect(lambda: self.view.zoom_by(1 / 1.2))
        self.fit_button.clicked.connect(self._fit_to_window)
        self.actual_button.clicked.connect(lambda: self._set_scale(1.0))
        self._render()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._fit_pending:
            self._fit_pending = False
            self._fit_to_window()

    def _set_scale(self, value: float) -> None:
        self.view.set_scale(value)

    def _fit_to_window(self) -> None:
        if self._pixmap.isNull():
            return
        self.view.fit_to_window()

    def _scale_changed(self, value: float) -> None:
        self._scale = value
        self.scale_label.setText(f"{round(value * 100)}%")

    def _render(self) -> None:
        if self._pixmap.isNull():
            self.scale_label.setText("图片无法读取")

    def _release_resources(self) -> None:
        if getattr(self, "_resources_released", False):
            return
        self._resources_released = True
        self.view.clear_pixmap_item()
        self.view.setScene(None)
        self.scene.clear()
        self.pixmap_item = None
        self._pixmap = QPixmap()

    def done(self, result: int) -> None:
        self._release_resources()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._release_resources()
        super().closeEvent(event)


class ImagePreviewPane(QWidget):
    selection_changed = Signal(str, object, object)

    def __init__(
        self,
        role: str,
        title: str,
        *,
        resolver: SharedPathResolver | None = None,
        icon_size: int = 72,
        async_thumbnails: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.role = role
        self.title = title
        self.resolver = resolver
        self.async_thumbnails = async_thumbnails
        self.items: list[tuple[Path, Path, str, object]] = []
        self._zoom_dialog: LargeImageDialog | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        self.title_label = QLabel(f"{title}（0）")
        self.title_label.setStyleSheet(
            "font-weight:700;color:#1f2d43;"
        )
        self.list_widget = ImageStrip(icon_size=icon_size)
        self.preview = ClickableImageView()
        self.preview.set_placeholder(f"选择{title}查看大图")
        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet("color:#60708a;font-size:11px;")
        layout.addWidget(self.title_label)
        layout.addWidget(self.list_widget)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.meta_label)
        self.list_widget.currentRowChanged.connect(self._row_changed)
        self.list_widget.itemClicked.connect(
            lambda item: self._row_changed(self.list_widget.row(item))
        )
        self.preview.clicked.connect(self._open_zoom)

    def set_resolver(self, resolver: SharedPathResolver | None) -> None:
        self.resolver = resolver

    def set_images(
        self,
        items: list[
            tuple[str | Path, str, object]
            | tuple[str | Path, str | Path, str, object]
        ],
        *,
        default_row: int | None = None,
    ) -> None:
        self.items = []
        for item in items:
            if len(item) == 4:
                thumbnail_path, image_path, label, payload = item
            else:
                thumbnail_path, label, payload = item
                image_path = thumbnail_path
            self.items.append(
                (
                    Path(thumbnail_path),
                    Path(image_path),
                    label,
                    payload,
                )
            )
        self.title_label.setText(f"{self.title}（{len(self.items)}）")
        thumbnail_items = [
            (thumbnail_path, label, payload)
            for thumbnail_path, _, label, payload in self.items
        ]
        if self.async_thumbnails:
            self.list_widget.set_images_async(thumbnail_items)
        else:
            self.list_widget.set_images(thumbnail_items)
        if not self.items:
            self.preview.set_placeholder(f"暂无{self.title}")
            self.preview.set_image(None)
            self.meta_label.clear()
            return
        row = 0 if default_row is None else default_row
        row = max(0, min(row, len(self.items) - 1))
        self.list_widget.setCurrentRow(row)
        self._row_changed(row)

    def set_task(self, task, results) -> None:
        if self.resolver is None:
            raise ValueError("图片面板未配置共享盘解析器")
        items = []
        for index, result in enumerate(results):
            thumbnail_path = self.resolver.resolve_project_path(
                task.project_relative_root,
                result.thumbnail_relative_path or result.relative_path,
            )
            image_path = self.resolver.resolve_project_path(
                task.project_relative_root,
                result.relative_path,
            )
            items.append(
                (
                    thumbnail_path,
                    image_path,
                    f"结果 {index + 1}",
                    result,
                )
            )
        self.set_images(items, default_row=0)

    def current_path(self) -> Path | None:
        return self.preview.current_path

    def current_payload(self):
        row = self.list_widget.currentRow()
        return (
            self.items[row][3]
            if 0 <= row < len(self.items)
            else None
        )

    def clear(self) -> None:
        self.items = []
        self.list_widget.clear()
        self.preview.set_placeholder(f"暂无{self.title}")
        self.preview.set_image(None)
        self.meta_label.clear()
        self.title_label.setText(f"{self.title}（0）")

    def _row_changed(self, row: int) -> None:
        if not 0 <= row < len(self.items):
            return
        _thumbnail_path, image_path, label, payload = self.items[row]
        self.preview.set_image(image_path)
        reader = QImageReader(str(image_path))
        size = reader.size()
        dimensions = (
            f"{size.width()} × {size.height()}"
            if size.isValid()
            else "尺寸未知"
        )
        self.meta_label.setText(f"{label} · {dimensions}")
        self.selection_changed.emit(self.role, payload, image_path)

    def _open_zoom(self) -> None:
        if self.preview.current_path is None:
            return
        self._zoom_dialog = LargeImageDialog(
            self.preview.current_path,
            self,
        )
        self._zoom_dialog.finished.connect(self._release_zoom)
        self._zoom_dialog.exec()

    def _release_zoom(self, _result: int) -> None:
        dialog = self._zoom_dialog
        self._zoom_dialog = None
        if dialog is not None:
            dialog.deleteLater()


class DualImagePreview(QWidget):
    selection_changed = Signal(str, object, object)

    def __init__(
        self,
        resolver: SharedPathResolver | None = None,
        async_thumbnails: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.result_pane = ImagePreviewPane(
            "result",
            "结果图",
            resolver=resolver,
            icon_size=72,
            async_thumbnails=async_thumbnails,
        )
        self.reference_pane = ImagePreviewPane(
            "reference",
            "参考图",
            resolver=resolver,
            icon_size=72,
            async_thumbnails=async_thumbnails,
        )
        self.splitter.addWidget(self.result_pane)
        self.splitter.addWidget(self.reference_pane)
        self.splitter.setSizes([1, 1])
        layout.addWidget(self.splitter)
        self.result_pane.selection_changed.connect(
            self.selection_changed.emit
        )
        self.reference_pane.selection_changed.connect(
            self.selection_changed.emit
        )

    def set_assets(
        self,
        results: list[
            tuple[str | Path, str, object]
            | tuple[str | Path, str | Path, str, object]
        ],
        references: list[
            tuple[str | Path, str, object]
            | tuple[str | Path, str | Path, str, object]
        ],
        *,
        default_reference_row: int = 0,
    ) -> None:
        self.result_pane.set_images(results)
        self.reference_pane.set_images(
            references,
            default_row=default_reference_row,
        )

    def set_resolver(self, resolver: SharedPathResolver | None) -> None:
        self.result_pane.set_resolver(resolver)
        self.reference_pane.set_resolver(resolver)

    def clear(self) -> None:
        self.result_pane.clear()
        self.reference_pane.clear()
