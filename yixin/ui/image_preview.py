from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImageReader,
    QPainter,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
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
    interacted = Signal()

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
        self.interacted.emit()
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
            self.interacted.emit()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)


class ImageZoomView(QWidget):
    activated = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_path: Path | None = None
        self._current_label = ""
        self._placeholder = "选择图片查看大图"
        self._scale = 1.0
        self._updating_scene = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self.fit_to_window)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scene = QGraphicsScene(self)
        self.view = ZoomableImageView(self.scene)
        self.view.scale_changed.connect(self._scale_changed)
        self.view.interacted.connect(self.activated)
        layout.addWidget(self.view, 1)
        self._render()

    @property
    def current_path(self) -> Path | None:
        return self._current_path

    @property
    def scale(self) -> float:
        return self._scale

    def set_image(
        self,
        path: str | Path | None,
        label: str = "",
    ) -> None:
        self._current_path = Path(path) if path is not None else None
        self._current_label = str(label)
        self._render()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        if self._current_path is None:
            self._render()

    def set_current_path(self, path: str | Path | None) -> None:
        self._current_path = Path(path) if path else None

    def fit_to_window(self) -> None:
        self.view.fit_to_window()

    def set_zoom(self, value: float) -> None:
        self.view.set_scale(value)

    def _scale_changed(self, value: float) -> None:
        self._scale = value

    def _render(self) -> None:
        if self._updating_scene:
            return
        self._updating_scene = True
        try:
            self.scene.clear()
            if self._current_path is None:
                text = self.scene.addSimpleText(self._placeholder)
                text.setBrush(QColor("#60708a"))
                text.setFont(QFont(self.font().family(), 12))
                rect = text.boundingRect()
                text.setPos(-rect.width() / 2, -rect.height() / 2)
                self.view.clear_pixmap_item()
                self.view.set_has_content(False)
                self.view.setSceneRect(rect.adjusted(-30, -30, 30, 30))
            else:
                self._render_image()
                self.view.set_has_content(True)
            if self._current_path is not None:
                self._fit_timer.start(0)
        finally:
            self._updating_scene = False

    def _render_image(self) -> None:
        assert self._current_path is not None
        pixmap = QPixmap(str(self._current_path))
        label_height = 28
        if self._current_label:
            text = QGraphicsSimpleTextItem(self._current_label)
            text.setBrush(QColor("#d8e2f0"))
            text.setFont(QFont(self.font().family(), 10))
            text.setPos(8, 0)
            self.scene.addItem(text)
        if pixmap.isNull():
            text = QGraphicsSimpleTextItem(
                f"图片无法读取\n{self._current_path.name}"
            )
            text.setBrush(QColor("#d8e2f0"))
            text.setPos(20, label_height + 20)
            self.scene.addItem(text)
            self.scene.setSceneRect(QRectF(0, 0, 420, 220))
            return
        item = QGraphicsPixmapItem(pixmap)
        item.setPos(0, label_height)
        self.scene.addItem(item)
        self.scene.setSceneRect(
            QRectF(
                0,
                0,
                max(1, pixmap.width()),
                label_height + max(1, pixmap.height()),
            )
        )


class ImagePreviewPane(QWidget):
    selection_changed = Signal(str, object, object)
    activated = Signal(str)

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
        self._current_index: int | None = None
        self._updating_images = False
        self.setObjectName("ImagePreviewPane")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._apply_active_style(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel(f"{title}（0）")
        self.title_label.setStyleSheet(
            "font-weight:700;color:#1f2d43;"
        )
        self.meta_label = QLabel("")
        self.meta_label.setAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )
        self.meta_label.setStyleSheet("color:#60708a;font-size:11px;")
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.meta_label)
        self.list_widget = ImageStrip(icon_size=icon_size)
        self.zoom_view = ImageZoomView()
        self.preview = self.zoom_view
        self.preview.set_placeholder(f"选择{title}查看大图")
        layout.addLayout(header)
        layout.addWidget(self.list_widget)
        layout.addWidget(self.preview, 1)
        self.list_widget.currentRowChanged.connect(self._row_changed)
        self.list_widget.itemClicked.connect(
            lambda item: self._row_changed(self.list_widget.row(item))
        )
        self.zoom_view.activated.connect(self._activated)

    def set_resolver(self, resolver: SharedPathResolver | None) -> None:
        self.resolver = resolver

    def has_image(self) -> bool:
        return bool(
            self._current_index is not None
            and 0 <= self._current_index < len(self.items)
        )

    def set_active(self, active: bool) -> None:
        self._apply_active_style(active)

    def _apply_active_style(self, active: bool) -> None:
        border = "#3f73cb" if active else "#dce5f2"
        self.setStyleSheet(
            "QWidget#ImagePreviewPane {"
            f"background:#ffffff;border:1px solid {border};"
            "border-radius:10px;"
            "}"
        )
        if hasattr(self, "title_label"):
            color = "#245ab8" if active else "#1f2d43"
            self.title_label.setStyleSheet(
                f"font-weight:700;color:{color};"
            )

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
            self._current_index = None
            self.preview.set_placeholder(f"暂无{self.title}")
            self.preview.set_image(None)
            self.meta_label.setText("暂无图片")
            self._updating_images = False
            return
        row = 0 if default_row is None else default_row
        row = max(0, min(row, len(self.items) - 1))
        self.list_widget.setCurrentRow(row)
        self._updating_images = False
        self._select_row(row)

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
        self._current_index = None
        self.list_widget.clear()
        self.preview.set_placeholder(f"暂无{self.title}")
        self.preview.set_image(None)
        self.meta_label.setText("暂无图片")
        self.title_label.setText(f"{self.title}（0）")
        self._updating_images = False

    def _row_changed(self, row: int) -> None:
        if self._updating_images:
            return
        self._select_row(row)

    def _select_row(self, row: int) -> None:
        if not 0 <= row < len(self.items):
            return
        self._current_index = row
        _thumbnail_path, image_path, label, payload = self.items[row]
        self.preview.set_image(image_path, label)
        reader = QImageReader(str(image_path))
        size = reader.size()
        dimensions = (
            f"{size.width()} × {size.height()}"
            if size.isValid()
            else "尺寸未知"
        )
        self.meta_label.setText(f"{label} · {dimensions}")
        self.selection_changed.emit(self.role, payload, image_path)
        self.activated.emit(self.role)

    def _activated(self) -> None:
        if self.has_image():
            self.activated.emit(self.role)


class DualImagePreview(QWidget):
    selection_changed = Signal(str, object, object)
    active_role_changed = Signal(str)

    def __init__(
        self,
        resolver: SharedPathResolver | None = None,
        async_thumbnails: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.active_role = "result"
        self._updating_images = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        toolbar_host = QWidget()
        toolbar_host.setFixedHeight(36)
        toolbar = QHBoxLayout(toolbar_host)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(4)
        self.active_label = QLabel("当前：结果图")
        self.active_label.setStyleSheet(
            "color:#315272;font-weight:600;"
        )
        self.zoom_in_button = QPushButton("放大")
        self.zoom_out_button = QPushButton("缩小")
        self.fit_button = QPushButton("适应窗口")
        self.actual_button = QPushButton("100%")
        self.scale_label = QLabel("100%")
        self.scale_label.setStyleSheet("color:#60708a;")
        toolbar.addWidget(self.active_label)
        toolbar.addStretch(1)
        for button in (
            self.zoom_in_button,
            self.zoom_out_button,
            self.fit_button,
            self.actual_button,
        ):
            toolbar.addWidget(button)
        toolbar.addWidget(self.scale_label)
        layout.addWidget(toolbar_host)
        self.zoom_in_button.clicked.connect(
            lambda: self._active_view().view.zoom_by(1.2)
        )
        self.zoom_out_button.clicked.connect(
            lambda: self._active_view().view.zoom_by(1 / 1.2)
        )
        self.fit_button.clicked.connect(
            lambda: self._active_view().fit_to_window()
        )
        self.actual_button.clicked.connect(
            lambda: self._active_view().set_zoom(1.0)
        )
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
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
        self.result_pane.activated.connect(self.set_active_role)
        self.reference_pane.activated.connect(self.set_active_role)
        self.result_pane.preview.view.scale_changed.connect(
            lambda value: self._pane_scale_changed("result", value)
        )
        self.reference_pane.preview.view.scale_changed.connect(
            lambda value: self._pane_scale_changed("reference", value)
        )
        self.set_active_role("result")

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
        self._updating_images = True
        self.result_pane.set_images(results)
        self.reference_pane.set_images(
            references,
            default_row=default_reference_row,
        )
        self._updating_images = False
        preferred = (
            "result"
            if self.result_pane.has_image()
            else "reference"
        )
        self.set_active_role(preferred)

    def set_resolver(self, resolver: SharedPathResolver | None) -> None:
        self.result_pane.set_resolver(resolver)
        self.reference_pane.set_resolver(resolver)

    def clear(self) -> None:
        self.result_pane.clear()
        self.reference_pane.clear()
        self.active_role = "result"
        self.result_pane.set_active(False)
        self.reference_pane.set_active(False)
        self.active_label.setText("当前：结果图")
        self.scale_label.setText("100%")
        self._update_toolbar_state()

    def set_active_role(self, role: str) -> None:
        if self._updating_images:
            return
        pane = self._pane_for_role(role)
        if pane is None or not pane.has_image():
            return
        self.active_role = role
        self.result_pane.set_active(role == "result")
        self.reference_pane.set_active(role == "reference")
        self.active_label.setText(
            f"当前：{'结果图' if role == 'result' else '参考图'}"
        )
        self.scale_label.setText(
            f"{round(pane.preview.scale * 100)}%"
        )
        self._update_toolbar_state()
        self.active_role_changed.emit(role)

    def _pane_for_role(
        self,
        role: str,
    ) -> ImagePreviewPane | None:
        if role == "result":
            return self.result_pane
        if role == "reference":
            return self.reference_pane
        return None

    def _active_view(self) -> ImageZoomView:
        pane = self._pane_for_role(self.active_role)
        if pane is None:
            return self.result_pane.preview
        return pane.preview

    def _pane_scale_changed(self, role: str, value: float) -> None:
        if role == self.active_role:
            self.scale_label.setText(f"{round(value * 100)}%")

    def _update_toolbar_state(self) -> None:
        pane = self._pane_for_role(self.active_role)
        enabled = bool(pane and pane.has_image())
        for button in (
            self.zoom_in_button,
            self.zoom_out_button,
            self.fit_button,
            self.actual_button,
        ):
            button.setEnabled(enabled)
