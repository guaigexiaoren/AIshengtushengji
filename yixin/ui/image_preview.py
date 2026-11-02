from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImageReader,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.shared_paths import SharedPathResolver
from .widgets import ImageStrip


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
        self._has_content = False
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

    def set_has_content(self, value: bool) -> None:
        self._has_content = value

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
        scene = self.scene()
        target_rect = (
            scene.itemsBoundingRect()
            if scene is not None and self._has_content
            else QRectF()
        )
        if not target_rect.isEmpty():
            viewport = self.viewport().size()
            self.setSceneRect(target_rect)
            fitted = min(
                max(1, viewport.width() - 16)
                / max(1.0, target_rect.width()),
                max(1, viewport.height() - 16)
                / max(1.0, target_rect.height()),
            )
            target = max(self.MIN_SCALE, min(self.MAX_SCALE, fitted))
            self.setTransform(QTransform().scale(target, target))
            self.centerOn(target_rect.center())
        else:
            target = 1.0
        self._scale = target
        self.scale_changed.emit(target)

    def wheelEvent(self, event) -> None:
        if not self._has_content:
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
            and self._has_content
        ):
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)


class ImageCompareView(QWidget):
    images_swapped = Signal()
    comparison_exited = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.images: list[tuple[Path, str]] = []
        self._current_path: Path | None = None
        self._placeholder = "选择图片查看大图"
        self._scale = 1.0
        self._updating_scene = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self.fit_to_window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        self.zoom_in_button = QPushButton("放大")
        self.zoom_out_button = QPushButton("缩小")
        self.fit_button = QPushButton("适应窗口")
        self.actual_button = QPushButton("100%")
        self.swap_button = QPushButton("交换左右")
        self.exit_compare_button = QPushButton("退出对比")
        self.scale_label = QLabel("100%")
        for button in (
            self.zoom_in_button,
            self.zoom_out_button,
            self.fit_button,
            self.actual_button,
        ):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.swap_button)
        toolbar.addWidget(self.exit_compare_button)
        toolbar.addWidget(self.scale_label)
        layout.addLayout(toolbar)
        self.scene = QGraphicsScene(self)
        self.view = ZoomableImageView(self.scene)
        self.view.scale_changed.connect(self._scale_changed)
        layout.addWidget(self.view, 1)
        self.zoom_in_button.clicked.connect(lambda: self.view.zoom_by(1.2))
        self.zoom_out_button.clicked.connect(
            lambda: self.view.zoom_by(1 / 1.2)
        )
        self.fit_button.clicked.connect(self.fit_to_window)
        self.actual_button.clicked.connect(lambda: self.set_zoom(1.0))
        self.swap_button.clicked.connect(self.swap_images)
        self.exit_compare_button.clicked.connect(self.exit_comparison)
        self._render()

    @property
    def current_path(self) -> Path | None:
        return self._current_path

    def set_images(
        self,
        images: list[tuple[str | Path, str]],
    ) -> None:
        self.images = [
            (Path(path), str(label))
            for path, label in images[:2]
        ]
        self._current_path = (
            self.images[-1][0] if self.images else None
        )
        self._render()

    def set_image(
        self,
        path: str | Path | None,
        label: str = "",
    ) -> None:
        self.set_images(
            [(path, label)] if path is not None else []
        )

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        if not self.images:
            self._render()

    def set_current_path(self, path: str | Path | None) -> None:
        self._current_path = Path(path) if path else None

    def fit_to_window(self) -> None:
        self.view.fit_to_window()

    def set_zoom(self, value: float) -> None:
        self.view.set_scale(value)

    def swap_images(self) -> None:
        if len(self.images) != 2:
            return
        self.images.reverse()
        self._current_path = self.images[-1][0]
        self._render()
        self.images_swapped.emit()

    def exit_comparison(self) -> None:
        if len(self.images) <= 1:
            return
        self.images = self.images[:1]
        self._current_path = self.images[0][0]
        self._render()
        self.comparison_exited.emit()

    def _scale_changed(self, value: float) -> None:
        self._scale = value
        self.scale_label.setText(f"{round(value * 100)}%")

    def _render(self) -> None:
        if self._updating_scene:
            return
        self._updating_scene = True
        try:
            self.scene.clear()
            pixmaps = [
                QPixmap(str(path))
                for path, _label in self.images
            ]
            if not self.images:
                text = self.scene.addSimpleText(self._placeholder)
                text.setBrush(QColor("#60708a"))
                text.setFont(QFont(self.font().family(), 12))
                rect = text.boundingRect()
                text.setPos(-rect.width() / 2, -rect.height() / 2)
                self.view.clear_pixmap_item()
                self.view.set_has_content(False)
                self.view.setSceneRect(rect.adjusted(-30, -30, 30, 30))
            else:
                self._render_images(pixmaps)
                self.view.set_has_content(True)
            self.swap_button.setEnabled(len(self.images) == 2)
            self.exit_compare_button.setEnabled(len(self.images) == 2)
            if self.images:
                self._fit_timer.start(0)
        finally:
            self._updating_scene = False

    def _render_images(self, pixmaps: list[QPixmap]) -> None:
        label_height = 28
        gap = 36
        widths = [max(1, pixmap.width()) for pixmap in pixmaps]
        heights = [max(1, pixmap.height()) for pixmap in pixmaps]
        cell_width = max(widths)
        cell_height = max(heights)
        total_width = (
            cell_width
            if len(pixmaps) == 1
            else cell_width * 2 + gap
        )
        for index, (pixmap, (path, label)) in enumerate(
            zip(pixmaps, self.images)
        ):
            cell_left = index * (cell_width + gap)
            slot_name = (
                ""
                if len(pixmaps) == 1
                else ("左图" if index == 0 else "右图")
            )
            display_label = (
                f"{slot_name} · {label}" if slot_name else label
            )
            if display_label:
                text = QGraphicsSimpleTextItem(display_label)
                text.setBrush(QColor("#d8e2f0"))
                text.setFont(QFont(self.font().family(), 10))
                text.setPos(cell_left + 8, 0)
                self.scene.addItem(text)
            if pixmap.isNull():
                text = QGraphicsSimpleTextItem(
                    f"图片无法读取\n{path.name}"
                )
                text.setBrush(QColor("#d8e2f0"))
                text.setPos(cell_left + 20, label_height + 20)
                self.scene.addItem(text)
                continue
            item = QGraphicsPixmapItem(pixmap)
            x = cell_left + (cell_width - pixmap.width()) / 2
            y = label_height + (cell_height - pixmap.height()) / 2
            item.setPos(x, y)
            self.scene.addItem(item)
        if len(pixmaps) == 2:
            separator = QGraphicsLineItem(
                cell_width + gap / 2,
                label_height,
                cell_width + gap / 2,
                label_height + cell_height,
            )
            separator.setPen(QPen(QColor("#64748b")))
            self.scene.addItem(separator)
        scene_width = total_width
        scene_height = label_height + cell_height
        self.scene.setSceneRect(
            QRectF(0, 0, scene_width, scene_height)
        )


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
        self._left_index: int | None = None
        self._right_index: int | None = None
        self._current_index: int | None = None
        self._updating_images = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        self.title_label = QLabel(f"{title}（0）")
        self.title_label.setStyleSheet(
            "font-weight:700;color:#1f2d43;"
        )
        self.list_widget = ImageStrip(icon_size=icon_size)
        self.compare_view = ImageCompareView()
        self.preview = self.compare_view
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
        self.compare_view.images_swapped.connect(
            self._comparison_swapped
        )
        self.compare_view.comparison_exited.connect(
            self._comparison_exited
        )

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
        self._updating_images = True
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
            self._left_index = None
            self._right_index = None
            self._current_index = None
            self.preview.set_placeholder(f"暂无{self.title}")
            self.preview.set_images([])
            self.meta_label.clear()
            self._updating_images = False
            return
        row = 0 if default_row is None else default_row
        row = max(0, min(row, len(self.items) - 1))
        self.list_widget.setCurrentRow(row)
        self._updating_images = False
        self._select_row(row, initial=True)

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
        row = (
            self._current_index
            if self._current_index is not None
            else self.list_widget.currentRow()
        )
        return (
            self.items[row][3]
            if 0 <= row < len(self.items)
            else None
        )

    def clear(self) -> None:
        self._updating_images = True
        self.items = []
        self._left_index = None
        self._right_index = None
        self._current_index = None
        self.list_widget.clear()
        self.preview.set_placeholder(f"暂无{self.title}")
        self.preview.set_images([])
        self.meta_label.clear()
        self.title_label.setText(f"{self.title}（0）")
        self._updating_images = False

    def _row_changed(self, row: int) -> None:
        if self._updating_images:
            return
        self._select_row(row)

    def _select_row(self, row: int, *, initial: bool = False) -> None:
        if not 0 <= row < len(self.items):
            return
        if initial or self._left_index is None:
            self._left_index = row
            self._right_index = None
        elif row == self._left_index:
            pass
        else:
            self._right_index = row
        self._current_index = row
        _thumbnail_path, image_path, label, payload = self.items[row]
        compare_images = [
            (
                self.items[self._left_index][1],
                self.items[self._left_index][2],
            )
        ]
        if (
            self._right_index is not None
            and self._right_index != self._left_index
        ):
            compare_images.append(
                (
                    self.items[self._right_index][1],
                    self.items[self._right_index][2],
                )
            )
        self.preview.set_images(compare_images)
        self.preview.set_current_path(image_path)
        reader = QImageReader(str(image_path))
        size = reader.size()
        dimensions = (
            f"{size.width()} × {size.height()}"
            if size.isValid()
            else "尺寸未知"
        )
        self.meta_label.setText(f"{label} · {dimensions}")
        self.selection_changed.emit(self.role, payload, image_path)

    def _comparison_swapped(self) -> None:
        if self._left_index is None or self._right_index is None:
            return
        self._left_index, self._right_index = (
            self._right_index,
            self._left_index,
        )
        self._current_index = self._right_index
        self._emit_current()

    def _comparison_exited(self) -> None:
        if self._left_index is None:
            return
        self._right_index = None
        self._current_index = self._left_index
        self._emit_current()

    def _emit_current(self) -> None:
        row = self._current_index
        if row is None or not 0 <= row < len(self.items):
            return
        image_path = self.items[row][1]
        payload = self.items[row][3]
        self.preview.set_current_path(image_path)
        self.selection_changed.emit(self.role, payload, image_path)


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
