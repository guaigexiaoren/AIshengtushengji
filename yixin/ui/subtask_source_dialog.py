from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import AuthUser, ProjectAssetInput, ProjectTask
from .widgets import load_scaled_pixmap


class SubtaskSourceDialog(QDialog):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        task: ProjectTask,
        assets: tuple[ProjectAssetInput, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.task = task
        self.project_root = resolver.project_root(
            task.project_relative_root
        )
        self.setWindowTitle(f"修改素材与配对 · {task.task_code}")
        self.resize(860, 620)
        layout = QVBoxLayout(self)
        tip = QLabel("修改后旧候选图会保留，但不能提交验收，需要重新生成。")
        tip.setStyleSheet("color:#9a5b22;")
        layout.addWidget(tip)
        columns = QHBoxLayout()
        self.garment_list = self._list_widget()
        self.model_list = self._list_widget(single=True)
        columns.addWidget(self._box("衣服图", self.garment_list), 1)
        columns.addWidget(self._box("模特图", self.model_list), 1)
        layout.addLayout(columns, 1)
        self._fill(assets)
        actions = QHBoxLayout()
        cancel = QPushButton("取消")
        save = QPushButton("保存修改")
        save.setObjectName("PrimaryButton")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    @staticmethod
    def _list_widget(*, single: bool = False) -> QListWidget:
        widget = QListWidget()
        widget.setViewMode(QListWidget.ViewMode.IconMode)
        widget.setIconSize(QSize(110, 110))
        widget.setGridSize(QSize(145, 145))
        widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
            if single
            else QAbstractItemView.SelectionMode.ExtendedSelection
        )
        return widget

    @staticmethod
    def _box(title: str, widget: QListWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.addWidget(QLabel(title))
        layout.addWidget(widget, 1)
        return box

    def _fill(self, assets: tuple[ProjectAssetInput, ...]) -> None:
        garment_paths = {asset.relative_path for asset in self.task.garment_assets}
        model_path = self.task.model_path
        for asset in assets:
            target = (
                self.garment_list
                if asset.role == "garment"
                else self.model_list
            )
            item = QListWidgetItem(asset.original_name)
            path = self.project_root / asset.relative_path
            pixmap = load_scaled_pixmap(path, QSize(110, 110))
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            item.setData(Qt.ItemDataRole.UserRole, asset.relative_path)
            target.addItem(item)
            if asset.relative_path in garment_paths and asset.role == "garment":
                item.setSelected(True)
            if asset.relative_path == model_path and asset.role == "model":
                item.setSelected(True)

    def _save(self) -> None:
        garment_paths = [
            str(item.data(Qt.ItemDataRole.UserRole))
            for item in self.garment_list.selectedItems()
        ]
        model_items = self.model_list.selectedItems()
        model_path = (
            str(model_items[0].data(Qt.ItemDataRole.UserRole))
            if model_items
            else ""
        )
        try:
            self.repository.update_subtask_sources(
                self.task.id,
                self.actor,
                garment_paths=garment_paths,
                model_path=model_path,
            )
        except Exception as exc:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()
