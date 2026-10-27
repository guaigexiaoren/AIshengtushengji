from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QImageReader
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
from .widgets import ImageView, load_scaled_pixmap


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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setWrapping(False)
        self.list_widget.setFlow(QListWidget.Flow.LeftToRight)
        self.list_widget.setIconSize(QSize(92, 92))
        self.list_widget.setGridSize(QSize(122, 140))
        self.list_widget.setSpacing(4)
        self.list_widget.setFixedHeight(146)
        layout.addWidget(self.list_widget)
        self.preview = ImageView()
        self.preview.set_placeholder("选择图片查看大图")
        self.preview.setMinimumSize(320, 300)
        self.meta = QLabel()
        self.meta.setWordWrap(True)
        self.meta.setStyleSheet("color:#60708a;")
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.meta)
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
            self.preview.set_placeholder("当前任务还没有生成图片")
            self.preview.set_image(None)
            self.meta.clear()

    def clear(self) -> None:
        self.task = None
        self.results = []
        self.paths.clear()
        self.list_widget.clear()
        self.preview.set_placeholder("选择图片查看大图")
        self.preview.set_image(None)
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

    def set_selected_result_ids(self, result_ids: tuple[int, ...]) -> None:
        selected = set(result_ids)
        self.list_widget.blockSignals(True)
        try:
            for index in range(self.list_widget.count()):
                item = self.list_widget.item(index)
                if not (
                    item.flags()
                    & Qt.ItemFlag.ItemIsUserCheckable
                ):
                    continue
                item.setCheckState(
                    Qt.CheckState.Checked
                    if int(item.data(Qt.ItemDataRole.UserRole))
                    in selected
                    else Qt.CheckState.Unchecked
                )
        finally:
            self.list_widget.blockSignals(False)

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
            self.preview.set_placeholder("图片文件不存在")
            self.preview.set_image(None)
            self.meta.setText(str(path or ""))
            return
        self.preview.set_image(path)
        reader = QImageReader(str(path))
        size = reader.size()
        dimensions = (
            f"{size.width()} × {size.height()}"
            if size.isValid()
            else "尺寸未知"
        )
        self.meta.setText(f"{current.text()} · {dimensions}")


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
