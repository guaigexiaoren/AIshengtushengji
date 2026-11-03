from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..constants import MODEL_LABELS
from ..database import Database
from ..library import LibraryService
from ..settings import AUTO_STRATEGY_LABELS
from .widgets import (
    SUPPORTED_REFERENCE_SUFFIXES,
    ImageStrip,
    ImageView,
    scan_image_inputs,
)
from .async_thumbnails import get_thumbnail_loader
from .components import PageActionBar, PageHeader


def _drop_inputs(mime) -> list[Path]:
    if not mime.hasUrls():
        return []
    paths = [
        Path(url.toLocalFile())
        for url in mime.urls()
        if url.isLocalFile()
    ]
    return [
        path
        for path in paths
        if path.is_dir() or path.suffix.lower() in SUPPORTED_REFERENCE_SUFFIXES
    ]


class LibraryDropFrame(QFrame):
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if _drop_inputs(event.mimeData()):
            self.setStyleSheet(
                "QFrame#Card { border:2px solid #3f73cb; background:#edf4ff;"
                " border-radius:12px; }"
            )
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if _drop_inputs(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        paths = _drop_inputs(event.mimeData())
        self.setStyleSheet("")
        if not paths:
            event.ignore()
            return
        self.files_dropped.emit(paths)
        event.acceptProposedAction()


class LibraryWorkList(QListWidget):
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)

    def dragEnterEvent(self, event) -> None:
        if _drop_inputs(event.mimeData()):
            self.setStyleSheet(
                "QListWidget#WorkList { border:2px solid #3f73cb;"
                " background:#edf4ff; border-radius:10px; }"
            )
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if _drop_inputs(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        paths = _drop_inputs(event.mimeData())
        self.setStyleSheet("")
        if not paths:
            event.ignore()
            return
        self.files_dropped.emit(paths)
        event.acceptProposedAction()


class LibraryPage(QWidget):
    def __init__(
        self,
        database: Database,
        library_service: LibraryService,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.library_service = library_service
        self.current_work_id: int | None = None
        self.current_assets: list[dict] = []
        self._refreshing_tags = False
        self._cover_generation = 0
        self._cover_contexts: dict[int, tuple[int, int]] = {}
        self._thumbnail_loader = None

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        root.addWidget(
            PageHeader(
                "作品库",
                "管理本机保存的生成结果、参考图和可复用素材。",
            )
        )
        root.addWidget(splitter, 1)

        left = LibraryDropFrame()
        self.left_card = left
        left_layout = QVBoxLayout(left)
        title_row = QHBoxLayout()
        title = QLabel("作品列表")
        title.setObjectName("SectionTitle")
        upload_images = QPushButton("上传图片")
        upload_folder = QPushButton("上传文件夹")
        upload_images.setObjectName("PrimaryButton")
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(upload_images)
        title_row.addWidget(upload_folder)
        left_layout.addLayout(title_row)
        drop_hint = QLabel("支持拖拽图片或文件夹到这里")
        drop_hint.setStyleSheet("color:#6d7a91;")
        left_layout.addWidget(drop_hint)
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索标题、提示词或标签")
        apply_button = QPushButton("筛选")
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(apply_button)
        left_layout.addLayout(search_row)
        left_layout.addWidget(QLabel("标签（勾选多个时需同时满足）"))
        self.tag_list = QListWidget()
        self.tag_list.setMaximumHeight(120)
        left_layout.addWidget(self.tag_list)
        self.work_list = LibraryWorkList()
        self.work_list.setObjectName("WorkList")
        self.work_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.work_list.setIconSize(QSize(190, 190))
        self.work_list.setGridSize(QSize(225, 245))
        self.work_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.work_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        left_layout.addWidget(self.work_list, 1)

        right = QFrame()
        right.setObjectName("Card")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail_content = QWidget()
        detail_content.setObjectName("CardContent")
        detail_layout = QVBoxLayout(detail_content)
        detail_layout.setContentsMargins(18, 16, 18, 16)
        detail_layout.setSpacing(10)
        top = QHBoxLayout()
        self.detail_title = QLabel("选择一个作品")
        self.detail_title.setObjectName("SectionTitle")
        self.delete_button = QPushButton("删除作品")
        self.delete_button.setObjectName("DangerButton")
        top.addWidget(self.detail_title, 1)
        detail_layout.addLayout(top)
        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet("color:#637086;")
        detail_layout.addWidget(self.meta_label)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("作品文件夹路径会显示在这里")
        self.open_folder_button = QPushButton("打开作品文件夹")
        path_row.addWidget(QLabel("保存位置"))
        path_row.addWidget(self.path_edit, 1)
        detail_layout.addLayout(path_row)
        prompt_title = QLabel("提示词")
        prompt_title.setObjectName("FieldLabel")
        detail_layout.addWidget(prompt_title)
        self.prompt_view = QTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setMaximumHeight(145)
        detail_layout.addWidget(self.prompt_view)
        tag_row = QHBoxLayout()
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("标签用逗号分隔，例如：人像, 春天, 商业海报")
        save_tags = QPushButton("保存标签")
        tag_row.addWidget(QLabel("标签"))
        tag_row.addWidget(self.tags_edit, 1)
        detail_layout.addLayout(tag_row)
        asset_title = QLabel("素材预览")
        asset_title.setObjectName("FieldLabel")
        detail_layout.addWidget(asset_title)
        self.asset_strip = ImageStrip(icon_size=78)
        self.asset_strip.setMinimumHeight(100)
        detail_layout.addWidget(self.asset_strip)
        self.preview = ImageView()
        self.preview.setMinimumHeight(280)
        detail_layout.addWidget(self.preview, 1)
        detail_scroll.setWidget(detail_content)
        right_layout.addWidget(detail_scroll)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([700, 700])
        action_bar = PageActionBar()
        action_bar.layout.addWidget(self.open_folder_button)
        action_bar.layout.addStretch(1)
        action_bar.layout.addWidget(save_tags)
        action_bar.layout.addWidget(self.delete_button)
        self.page_action_bar = action_bar
        root.addWidget(action_bar)

        apply_button.clicked.connect(self.refresh)
        self.search_edit.returnPressed.connect(self.refresh)
        self.tag_list.itemChanged.connect(self._tags_changed)
        self.work_list.itemSelectionChanged.connect(self._selection_changed)
        self.asset_strip.currentRowChanged.connect(self._show_asset)
        self.delete_button.clicked.connect(self._delete_current)
        self.open_folder_button.clicked.connect(self._open_work_folder)
        save_tags.clicked.connect(self._save_tags)
        upload_images.clicked.connect(self._upload_image_files)
        upload_folder.clicked.connect(self._upload_image_folder)
        left.files_dropped.connect(self._import_manual_inputs)
        self.work_list.files_dropped.connect(self._import_manual_inputs)
        if auto_load:
            self.refresh()

    def _upload_image_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "上传图片到作品库",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;所有文件 (*)",
        )
        if paths:
            self._import_manual_inputs([Path(path) for path in paths])

    def _upload_image_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "上传文件夹到作品库",
        )
        if directory:
            self._import_manual_inputs([Path(directory)])

    def _import_manual_inputs(self, inputs: list[Path]) -> None:
        paths, scan_errors = scan_image_inputs(inputs)
        if not paths:
            details = "\n".join(scan_errors[:8])
            if details:
                QMessageBox.warning(self, "没有可导入图片", details)
            else:
                QMessageBox.information(
                    self,
                    "没有可导入图片",
                    "所选位置中没有支持的图片。",
                )
            return
        summary = self.library_service.import_images(paths)
        errors = [*scan_errors, *summary.errors]
        if summary.work_ids:
            self.current_work_id = summary.work_ids[-1]
            self.refresh()
            status_bar = getattr(self.window(), "statusBar", None)
            if callable(status_bar):
                status_bar().showMessage(
                    f"已导入 {len(summary.work_ids)} 件作品",
                    5000,
                )
        if errors:
            QMessageBox.warning(
                self,
                "部分图片未导入",
                f"成功 {len(summary.work_ids)} 张，失败 {len(errors)} 张。\n"
                + "\n".join(errors[:8]),
            )
        elif not summary.work_ids:
            QMessageBox.information(self, "导入失败", "没有图片导入成功。")

    def refresh(self) -> None:
        self._cover_generation += 1
        generation = self._cover_generation
        self._cover_contexts.clear()
        checked_tags = self._checked_tags()
        works = self.database.list_works(
            search=self.search_edit.text(),
            tags=checked_tags,
        )
        selected_id = self.current_work_id
        self.work_list.blockSignals(True)
        self.work_list.clear()
        selected_item = None
        for work in works:
            item = QListWidgetItem(work["title"])
            item.setData(Qt.ItemDataRole.UserRole, work)
            cover = work.get("cover_path")
            if cover and Path(cover).is_file():
                if self._thumbnail_loader is None:
                    self._thumbnail_loader = get_thumbnail_loader()
                    self._thumbnail_loader.thumbnail_ready.connect(
                        self._cover_thumbnail_ready
                    )
                    self._thumbnail_loader.thumbnail_failed.connect(
                        self._cover_thumbnail_failed
                    )
                request = self._thumbnail_loader.request_thumbnail(
                    cover,
                    QSize(190, 190),
                )
                self._cover_contexts[request.request_id] = (
                    int(work["id"]),
                    generation,
                )
            item.setToolTip(work["title"])
            self.work_list.addItem(item)
            if work["id"] == selected_id:
                selected_item = item
        if selected_item:
            selected_item.setSelected(True)
        self.work_list.blockSignals(False)
        self._refresh_tags()
        self._selection_changed()

    def _cover_thumbnail_ready(
        self,
        request_id: int,
        _path: str,
        image,
    ) -> None:
        context = self._cover_contexts.pop(request_id, None)
        if context is None:
            return
        work_id, generation = context
        if generation != self._cover_generation:
            return
        for index in range(self.work_list.count()):
            item = self.work_list.item(index)
            work = item.data(Qt.ItemDataRole.UserRole)
            if int(work["id"]) != work_id:
                continue
            pixmap = QPixmap.fromImage(
                image.scaled(
                    QSize(190, 190),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            return

    def _cover_thumbnail_failed(
        self,
        request_id: int,
        _path: str,
        _message: str,
    ) -> None:
        self._cover_contexts.pop(request_id, None)

    def _refresh_tags(self) -> None:
        selected = set(self._checked_tags())
        tags = self.database.list_tags()
        existing = [self.tag_list.item(i).text() for i in range(self.tag_list.count())]
        if existing == tags:
            return
        self._refreshing_tags = True
        self.tag_list.clear()
        for tag in tags:
            item = QListWidgetItem(tag)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if tag in selected else Qt.CheckState.Unchecked
            )
            self.tag_list.addItem(item)
        self._refreshing_tags = False

    def _checked_tags(self) -> list[str]:
        tags: list[str] = []
        for index in range(self.tag_list.count()):
            item = self.tag_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                tags.append(item.text())
        return tags

    def _tags_changed(self, _item: QListWidgetItem) -> None:
        if not self._refreshing_tags:
            self.refresh()

    def _selection_changed(self) -> None:
        selected = self.work_list.selectedItems()
        if not selected:
            self.current_work_id = None
            self.detail_title.setText("选择一个作品")
            self.meta_label.setText("作品详情、标签和保存位置会显示在这里。")
            self.prompt_view.clear()
            self.prompt_view.setPlaceholderText("")
            self.tags_edit.clear()
            self.path_edit.clear()
            self.asset_strip.clear()
            self.preview.set_image(None)
            self.preview.set_placeholder("作品详情会显示在这里")
            self.delete_button.setEnabled(False)
            self.open_folder_button.setEnabled(False)
            return
        work = selected[0].data(Qt.ItemDataRole.UserRole)
        self.show_work(work["id"])

    def show_work(self, work_id: int) -> None:
        work = self.database.get_work(work_id)
        if work is None:
            return
        self.current_work_id = work_id
        self.detail_title.setText(work["title"])
        params = self.library_service.work_parameters(work)
        if work["model"] == "手动上传":
            self.meta_label.setText(
                f"手动上传 · 原图 {params.get('width', '—')}×{params.get('height', '—')}"
                f" · {work['created_at'].replace('T', ' ')[:16]}"
            )
        else:
            model_name = MODEL_LABELS.get(work["model"], work["model"])
            strategy = params.get("auto_strategy")
            auto_text = (
                f" · 自动策略 {AUTO_STRATEGY_LABELS.get(strategy, strategy)}"
                if strategy
                else ""
            )
            self.meta_label.setText(
                f"{model_name}{auto_text} · 宽高比 {params.get('aspect_ratio', '—')}"
                f" · 分辨率 {params.get('resolution') or '自动'} · 数量 {params.get('n', '—')}"
                f" · {work['created_at'].replace('T', ' ')[:16]}"
            )
        if work["prompt"]:
            self.prompt_view.setPlainText(work["prompt"])
            self.prompt_view.setPlaceholderText("")
        else:
            self.prompt_view.clear()
            self.prompt_view.setPlaceholderText("手动上传作品没有生成提示词")
        self.tags_edit.setText(", ".join(self.database.get_work_tags(work_id)))
        self.delete_button.setEnabled(True)
        try:
            folder = self.library_service.work_folder(work_id)
            self.path_edit.setText(str(folder))
            self.path_edit.setCursorPosition(0)
            self.path_edit.setToolTip(str(folder))
            self.open_folder_button.setEnabled(folder.is_dir())
        except ValueError:
            self.path_edit.setText("作品文件夹不存在")
            self.path_edit.setToolTip("")
            self.open_folder_button.setEnabled(False)
        self.current_assets = [
            asset
            for asset in self.database.get_work_assets(work_id)
            if asset["role"] in ("result", "reference")
        ]
        items = []
        result_number = 0
        reference_number = 0
        for asset in self.current_assets:
            if asset["role"] == "result":
                result_number += 1
                label = f"结果 {result_number}"
            else:
                reference_number += 1
                label = f"参考 {reference_number}"
            items.append((Path(asset["path"]), label, asset))
        self.asset_strip.set_images(items)
        if self.current_assets:
            self._show_asset(0)

    def _show_asset(self, row: int) -> None:
        if 0 <= row < len(self.current_assets):
            self.preview.set_image(self.current_assets[row]["path"])

    def _save_tags(self) -> None:
        if self.current_work_id is None:
            return
        names = [name.strip() for name in self.tags_edit.text().split(",")]
        self.database.set_work_tags(self.current_work_id, names)
        self.refresh()

    def _open_work_folder(self) -> None:
        if self.current_work_id is None:
            return
        try:
            folder = self.library_service.work_folder(self.current_work_id)
        except ValueError as exc:
            QMessageBox.warning(self, "无法打开", str(exc))
            return
        if not folder.is_dir():
            QMessageBox.warning(self, "文件夹不存在", str(folder))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(self, "无法打开", f"系统未能打开资源管理器：\n{folder}")

    def _delete_current(self) -> None:
        if self.current_work_id is None:
            return
        work = self.database.get_work(self.current_work_id)
        if work is None:
            return
        answer = QMessageBox.warning(
            self,
            "删除作品",
            f"确定删除作品“{work['title']}”吗？\n本地作品图片、参考图和缩略图都会删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.library_service.delete_work(self.current_work_id)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self.current_work_id = None
        self.refresh()
