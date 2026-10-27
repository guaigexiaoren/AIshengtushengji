from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import AuthUser, ProjectAssetInput, ProjectTask
from ..storage import (
    SUPPORTED_IMAGE_SUFFIXES,
    atomic_copy_file,
    image_metadata,
    sha256_bytes,
)
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
        self.assets = list(assets)
        self.pending_sources: dict[str, Path] = {}
        self.setWindowTitle(f"修改素材与配对 · {task.task_code}")
        self.resize(860, 620)
        layout = QVBoxLayout(self)
        tip = QLabel("修改后旧候选图会保留，但不能提交验收，需要重新生成。")
        tip.setStyleSheet("color:#9a5b22;")
        layout.addWidget(tip)
        columns = QHBoxLayout()
        self.garment_list = self._list_widget()
        self.model_list = self._list_widget(single=True)
        columns.addWidget(
            self._box("衣服图", self.garment_list, "garment"),
            1,
        )
        columns.addWidget(
            self._box("模特图", self.model_list, "model"),
            1,
        )
        layout.addLayout(columns, 1)
        self._fill()
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

    def _box(
        self,
        title: str,
        widget: QListWidget,
        role: str,
    ) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.addWidget(QLabel(title))
        buttons = QHBoxLayout()
        add_files = QPushButton("添加图片")
        add_folder = QPushButton("添加文件夹")
        add_files.clicked.connect(
            lambda: self._add_files(role)
        )
        add_folder.clicked.connect(
            lambda: self._add_folder(role)
        )
        buttons.addWidget(add_files)
        buttons.addWidget(add_folder)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addWidget(widget, 1)
        return box

    def _fill(self) -> None:
        self.garment_list.clear()
        self.model_list.clear()
        garment_paths = {asset.relative_path for asset in self.task.garment_assets}
        model_path = self.task.model_path
        for asset in self.assets:
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

    def _add_files(self, role: str) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择衣服图片" if role == "garment" else "选择模特图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        if paths:
            self._stage_sources(role, [Path(path) for path in paths])

    def _add_folder(self, role: str) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择衣服图片文件夹" if role == "garment" else "选择模特图片文件夹",
        )
        if not folder:
            return
        paths = sorted(
            (
                path
                for path in Path(folder).rglob("*")
                if path.is_file()
                and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
            ),
            key=lambda path: str(path).casefold(),
        )
        self._stage_sources(role, paths)

    def _stage_sources(self, role: str, paths: list[Path]) -> None:
        target = self.garment_list if role == "garment" else self.model_list
        for source in paths:
            try:
                source = source.expanduser().resolve()
                if source.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                    continue
                image_metadata(source.read_bytes())
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "图片不可用",
                    f"{source.name}：{exc}",
                )
                continue
            key = f"pending:{uuid.uuid4().hex}"
            self.pending_sources[key] = source
            item = QListWidgetItem(source.name)
            pixmap = load_scaled_pixmap(source, QSize(110, 110))
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setSelected(True)
            target.addItem(item)

    def _save(self) -> None:
        model_items = self.model_list.selectedItems()
        if len(model_items) != 1:
            QMessageBox.information(
                self,
                "请选择模特图",
                "请选择且只选择一张模特图。",
            )
            return
        selected_items = [
            *((item, "garment") for item in self.garment_list.selectedItems()),
            *((item, "model") for item in model_items),
        ]
        copied_paths: list[Path] = []
        new_assets: list[ProjectAssetInput] = []
        selected_paths: dict[str, str] = {}
        try:
            for item, role in selected_items:
                key = str(item.data(Qt.ItemDataRole.UserRole))
                if not key.startswith("pending:"):
                    selected_paths[key] = key
                    continue
                source = self.pending_sources[key]
                destination, copied = self._copy_to_shared(source, role)
                if copied:
                    copied_paths.append(destination)
                asset = self._asset_input(destination, role)
                new_assets.append(asset)
                selected_paths[key] = asset.relative_path
            garment_paths = [
                selected_paths[str(item.data(Qt.ItemDataRole.UserRole))]
                for item in self.garment_list.selectedItems()
            ]
            model_path = selected_paths[
                str(model_items[0].data(Qt.ItemDataRole.UserRole))
            ]
            self.repository.update_subtask_sources(
                self.task.id,
                self.actor,
                garment_paths=garment_paths,
                model_path=model_path,
                new_assets=tuple(new_assets),
            )
        except Exception as exc:
            for path in copied_paths:
                try:
                    path.unlink()
                except OSError:
                    pass
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()

    def _copy_to_shared(
        self,
        source: Path,
        role: str,
    ) -> tuple[Path, bool]:
        folder_name = "衣服图" if role == "garment" else "模特图"
        folder = self.project_root / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        if source.parent == folder.resolve():
            return source, False
        destination = folder / source.name
        sequence = 2
        while destination.exists():
            destination = folder / f"{source.stem}_{sequence}{source.suffix}"
            sequence += 1
        atomic_copy_file(source, destination)
        return destination, True

    def _asset_input(
        self,
        path: Path,
        role: str,
    ) -> ProjectAssetInput:
        data = path.read_bytes()
        mime, width, height = image_metadata(data)
        return ProjectAssetInput(
            role=role,
            relative_path=path.relative_to(self.project_root).as_posix(),
            original_name=path.name,
            content_hash=sha256_bytes(data),
            mime=mime,
            width=width,
            height=height,
        )
