from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import GenerationResult, ProjectTask
from .widgets import load_scaled_pixmap


class ResultGallery(QWidget):
    def __init__(
        self,
        resolver: SharedPathResolver,
        *,
        checkable: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.resolver = resolver
        self.checkable = checkable
        self.task: ProjectTask | None = None
        self.results: list[GenerationResult] = []
        self.paths: dict[int, Path] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setIconSize(QSize(140, 140))
        self.list_widget.setGridSize(QSize(170, 190))
        self.list_widget.setSpacing(8)
        layout.addWidget(self.list_widget, 1)
        preview_host = QWidget()
        preview_layout = QVBoxLayout(preview_host)
        self.preview = QLabel("选择图片查看大图")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(320, 320)
        self.preview.setStyleSheet(
            "background:#f3f6fb;border:1px solid #dbe3ef;border-radius:8px;"
        )
        self.meta = QLabel()
        self.meta.setWordWrap(True)
        preview_layout.addWidget(self.preview, 1)
        preview_layout.addWidget(self.meta)
        layout.addWidget(preview_host, 1)
        self.list_widget.currentItemChanged.connect(self._show_current)

    def set_task(
        self,
        task: ProjectTask,
        results: list[GenerationResult],
    ) -> None:
        self.task = task
        self.results = results
        self.list_widget.clear()
        self.paths.clear()
        for result in results:
            preview_relative = (
                result.thumbnail_relative_path or result.relative_path
            )
            preview_path = self.resolver.resolve_project_path(
                task.project_relative_root,
                preview_relative,
            )
            image_path = self.resolver.resolve_project_path(
                task.project_relative_root,
                result.relative_path,
            )
            source_label = (
                "PS成品"
                if result.source_type == "manual_ps"
                else "AI生成"
            )
            stale = result.source_revision != task.source_revision
            item = QListWidgetItem(
                f"{source_label} · 第 {result.result_index + 1} 张"
                + (" · 最终" if result.is_final else "")
                + (" · 素材已变更" if stale else "")
            )
            pixmap = load_scaled_pixmap(preview_path, QSize(140, 140))
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            item.setData(Qt.ItemDataRole.UserRole, result.id)
            if self.checkable:
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                if stale:
                    item.setFlags(
                        item.flags()
                        & ~Qt.ItemFlag.ItemIsUserCheckable
                    )
                item.setCheckState(
                    Qt.CheckState.Checked
                    if result.is_final and not stale
                    else Qt.CheckState.Unchecked
                )
            if stale:
                item.setToolTip("素材已变更，此图仅保留为历史，不能提交验收。")
            self.paths[result.id] = image_path
            self.list_widget.addItem(item)
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        else:
            self.preview.setText("当前任务还没有生成图片")
            self.meta.clear()

    def clear(self) -> None:
        self.task = None
        self.results = []
        self.paths.clear()
        self.list_widget.clear()
        self.preview.setText("选择图片查看大图")
        self.meta.clear()

    def selected_result_ids(self) -> list[int]:
        selected: list[int] = []
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if (
                item.flags() & Qt.ItemFlag.ItemIsUserCheckable
                and item.checkState() == Qt.CheckState.Checked
            ):
                selected.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return selected

    def current_path(self) -> Path | None:
        current = self.list_widget.currentItem()
        if current is None:
            return None
        return self.paths.get(int(current.data(Qt.ItemDataRole.UserRole)))

    def current_result(self) -> GenerationResult | None:
        current = self.list_widget.currentItem()
        if current is None:
            return None
        result_id = int(current.data(Qt.ItemDataRole.UserRole))
        return next(
            (result for result in self.results if result.id == result_id),
            None,
        )

    def _show_current(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        result_id = int(current.data(Qt.ItemDataRole.UserRole))
        path = self.paths.get(result_id)
        if path is None or not path.is_file():
            self.preview.setText("图片文件不存在")
            self.meta.setText(str(path or ""))
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.preview.setText("无法读取图片")
        else:
            self.preview.setPixmap(
                pixmap.scaled(
                    self.preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.meta.setText(f"{path}\n{pixmap.width()} × {pixmap.height()}")


class ResultGalleryDialog(QDialog):
    def __init__(
        self,
        resolver: SharedPathResolver,
        task: ProjectTask,
        results: list[GenerationResult],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.resolver = resolver
        self.task = task
        self.setWindowTitle(f"{task.task_code} 生成结果")
        self.resize(940, 640)
        layout = QVBoxLayout(self)
        self.gallery = ResultGallery(resolver)
        self.gallery.set_task(task, results)
        layout.addWidget(self.gallery, 1)
        footer = QHBoxLayout()
        compare_button = QPushButton("与原模特图对比")
        compare_button.clicked.connect(self._compare_with_model)
        footer.addStretch(1)
        footer.addWidget(compare_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    def _compare_with_model(self) -> None:
        result_path = self.gallery.current_path()
        if result_path is None:
            return
        model_path = self.resolver.resolve_project_path(
            self.task.project_relative_root,
            self.task.model_path,
        )
        from .dialogs import CompareDialog

        CompareDialog(
            model_path,
            result_path,
            left_label="原模特图",
            right_label="生成成品",
            parent=self,
        ).exec()
