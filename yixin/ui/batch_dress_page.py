from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QPoint, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QDrag, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..constants import (
    AUTO_MODEL_ID,
    ASPECT_RATIOS,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MODEL,
    DEFAULT_RESOLUTION,
    MAX_BATCH_ASSETS,
    MODE_DRESS_UP,
    MODE_FREE_COMPOSE,
    MAX_REFERENCE_IMAGES,
    MODEL_SELECTION_OPTIONS,
    RESOLUTIONS,
)
from ..database import Database
from ..library import LibraryService
from ..model_router import choose_model
from ..models import ImageRequest, StoredImage
from ..security import SecretStore
from ..settings import AppSettings, INPUT_BATCH_GARMENT, INPUT_BATCH_PERSON
from ..storage import FileStore, ImageValidationError
from ..worker import TaskWorker
from .crop import CropDialog
from .dialogs import LibraryPickerDialog, TemplateManagerDialog
from .widgets import (
    load_scaled_pixmap,
    scan_image_inputs,
)

DRESS_UP_MIME = "application/x-yixin-batch-dress-up-garment"
PAIR_CARD_MIN_WIDTH = 280
PAIR_CARD_GAP = 8
MIN_PAIR_COLUMNS = 2
MAX_PAIR_COLUMNS = 8
ROLE_LABELS = {
    "garment": "服装",
    "person": "人物",
}
GROUP_KIND_LABELS = {
    "manual": "配对",
    "free": "自由组合",
    "auto": "自动组",
}


def _drop_local_paths(mime) -> list[Path]:
    if not mime.hasUrls():
        return []
    return [
        Path(url.toLocalFile())
        for url in mime.urls()
        if url.isLocalFile()
        and (Path(url.toLocalFile()).is_file() or Path(url.toLocalFile()).is_dir())
    ]


@dataclass(frozen=True)
class DressTaskGroup:
    references: tuple[StoredImage, ...]
    roles: tuple[str, ...]
    kind: str
    auto_prompt: bool = True
    skipped: bool = False

    @property
    def key(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (role, image.content_hash)
            for role, image in zip(self.roles, self.references)
        )

    @property
    def garment(self) -> StoredImage:
        return next(
            (
                image
                for role, image in zip(self.roles, self.references)
                if role == "garment"
            ),
            self.references[0],
        )

    @property
    def person(self) -> StoredImage:
        return next(
            (
                image
                for role, image in zip(self.roles, self.references)
                if role == "person"
            ),
            self.references[-1],
        )

    @property
    def garment_count(self) -> int:
        return self.roles.count("garment")

    @classmethod
    def manual(cls, garment: StoredImage, person: StoredImage) -> DressTaskGroup:
        return cls(
            references=(garment, person),
            roles=("garment", "person"),
            kind="manual",
            auto_prompt=False,
        )


DressPair = DressTaskGroup


class BatchAssetList(QListWidget):
    files_dropped = Signal(object)
    garment_dropped = Signal(str, int)

    def __init__(self, role: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self.setObjectName("ImageStrip")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setIconSize(QSize(82, 82))
        self.setGridSize(QSize(112, 126))
        self.setMinimumHeight(160)
        self.setAcceptDrops(True)
        self.setDragEnabled(False)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setWordWrap(True)
        self._drag_start: QPoint | None = None
        self._drag_row: int | None = None
        self._drag_active = False
        self.viewport().installEventFilter(self)
        self.installEventFilter(self)

    def refresh_assets(self, assets: Sequence[StoredImage]) -> None:
        selected_hash = None
        current = self.currentItem()
        if current:
            selected = current.data(Qt.ItemDataRole.UserRole)
            if isinstance(selected, StoredImage):
                selected_hash = selected.content_hash

        self.blockSignals(True)
        self.clear()
        for index, image in enumerate(assets):
            item = QListWidgetItem(
                f"{ROLE_LABELS[self.role]} {index + 1}\n{image.original_name}"
            )
            pixmap = load_scaled_pixmap(image.path, self.iconSize())
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            item.setData(Qt.ItemDataRole.UserRole, image)
            item.setToolTip(
                f"{ROLE_LABELS[self.role]} {index + 1}\n"
                f"{image.original_name}\n{image.path}"
            )
            self.addItem(item)
            if selected_hash and image.content_hash == selected_hash:
                self.setCurrentItem(item)
        self.blockSignals(False)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        if self.role != "garment" or event.button() != Qt.MouseButton.LeftButton:
            self._reset_drag()
            return
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            self._drag_row = index.row()
            self._drag_start = event.position().toPoint()
            self._drag_active = False
        else:
            self._reset_drag()

    def mouseMoveEvent(self, event) -> None:
        position = event.position().toPoint()
        if (
            self.role == "garment"
            and self._drag_row is not None
            and self._drag_start is not None
        ):
            if (
                not self._drag_active
                and (position - self._drag_start).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                self._drag_active = True
                item = self.item(self._drag_row)
                image = item.data(Qt.ItemDataRole.UserRole) if item else None
                if isinstance(image, StoredImage):
                    drag = QDrag(self)
                    mime = QMimeData()
                    mime.setData(DRESS_UP_MIME, image.content_hash.encode("utf-8"))
                    mime.setText(image.original_name)
                    drag.setMimeData(mime)
                    pixmap = item.icon().pixmap(self.iconSize())
                    if not pixmap.isNull():
                        drag.setPixmap(pixmap)
                        drag.setHotSpot(pixmap.rect().center())
                    drag.exec(Qt.DropAction.CopyAction)
                self._reset_drag()
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._reset_drag()
        super().mouseReleaseEvent(event)

    def _reset_drag(self) -> None:
        self._drag_start = None
        self._drag_row = None
        self._drag_active = False

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasUrls() or (
            self.role == "person" and mime.hasFormat(DRESS_UP_MIME)
        ):
            self._set_drop_highlight(True)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasUrls():
            event.acceptProposedAction()
            return
        if self.role == "person" and mime.hasFormat(DRESS_UP_MIME):
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                self.setCurrentRow(index.row())
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_highlight(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._set_drop_highlight(False)
        mime = event.mimeData()
        if self.role == "person" and mime.hasFormat(DRESS_UP_MIME):
            index = self.indexAt(event.position().toPoint())
            if not index.isValid():
                event.ignore()
                return
            payload = bytes(mime.data(DRESS_UP_MIME)).decode("utf-8")
            self.garment_dropped.emit(payload, index.row())
            event.acceptProposedAction()
            return
        if mime.hasUrls():
            paths = [
                Path(url.toLocalFile())
                for url in mime.urls()
                if url.isLocalFile()
            ]
            if not paths:
                event.ignore()
                return
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        event.ignore()

    def _set_drop_highlight(self, active: bool) -> None:
        if active:
            self.setStyleSheet(
                "QListWidget#ImageStrip { border: 2px solid #3f73cb;"
                " background: #edf4ff; }"
            )
        else:
            self.setStyleSheet("")

    def eventFilter(self, watched, event) -> bool:
        if watched is self or watched is self.viewport():
            if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                if _drop_local_paths(event.mimeData()):
                    self._set_drop_highlight(True)
                    event.acceptProposedAction()
                    return True
            elif event.type() == QEvent.Type.Drop:
                paths = _drop_local_paths(event.mimeData())
                self._set_drop_highlight(False)
                if paths:
                    self.files_dropped.emit(paths)
                    event.acceptProposedAction()
                    return True
            elif event.type() == QEvent.Type.DragLeave:
                self._set_drop_highlight(False)
        return super().eventFilter(watched, event)


class BatchAssetDropGroup(QGroupBox):
    files_dropped = Signal(list)

    def dragEnterEvent(self, event) -> None:
        if _drop_local_paths(event.mimeData()):
            self.setStyleSheet(
                "QGroupBox { border:2px solid #3f73cb; border-radius:10px;"
                " background:#edf4ff; }"
            )
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if _drop_local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        paths = _drop_local_paths(event.mimeData())
        self.setStyleSheet("")
        if not paths:
            event.ignore()
            return
        self.files_dropped.emit(paths)
        event.acceptProposedAction()


class FreeComposeDialog(QDialog):
    def __init__(
        self,
        garment_assets: Sequence[StoredImage],
        person_assets: Sequence[StoredImage],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("自由组合参考图")
        self.resize(920, 620)
        layout = QVBoxLayout(self)
        tip = QLabel(
            "从服装和人物素材中任选图片，按右侧顺序发送给模型。最多 14 张。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)
        content = QHBoxLayout()

        available_host = QGroupBox("可用素材")
        available_layout = QVBoxLayout(available_host)
        self.available_list = QListWidget()
        self.available_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.available_list.setIconSize(QSize(76, 76))
        self.available_list.setGridSize(QSize(120, 120))
        self.available_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.available_list.setMovement(QListWidget.Movement.Static)
        self.available_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        for role, assets in (
            ("garment", garment_assets),
            ("person", person_assets),
        ):
            for index, image in enumerate(assets, start=1):
                item = QListWidgetItem(
                    f"{ROLE_LABELS[role]} {index}\n{image.original_name}"
                )
                pixmap = load_scaled_pixmap(image.path, QSize(74, 74))
                if not pixmap.isNull():
                    item.setIcon(QIcon(pixmap))
                item.setData(Qt.ItemDataRole.UserRole, (role, image))
                item.setToolTip(f"{ROLE_LABELS[role]}：{image.original_name}")
                self.available_list.addItem(item)
        available_layout.addWidget(self.available_list)
        add_button = QPushButton("加入组合 →")
        add_button.setObjectName("PrimaryButton")
        available_layout.addWidget(add_button)
        content.addWidget(available_host, 1)

        selected_host = QGroupBox("当前组合（按顺序发送）")
        selected_layout = QVBoxLayout(selected_host)
        self.selected_list = QListWidget()
        self.selected_list.setIconSize(QSize(38, 38))
        self.selected_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection
        )
        selected_layout.addWidget(self.selected_list, 1)
        count = QLabel("已选择 0 / 14 张")
        self.count_label = count
        selected_layout.addWidget(count)
        sort_row = QHBoxLayout()
        move_up = QPushButton("上移")
        move_down = QPushButton("下移")
        remove = QPushButton("移除")
        sort_row.addWidget(move_up)
        sort_row.addWidget(move_down)
        sort_row.addWidget(remove)
        sort_row.addStretch(1)
        selected_layout.addLayout(sort_row)
        content.addWidget(selected_host, 1)
        layout.addLayout(content, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = QPushButton("取消")
        create = QPushButton("创建组合")
        create.setObjectName("PrimaryButton")
        footer.addWidget(cancel)
        footer.addWidget(create)
        layout.addLayout(footer)

        add_button.clicked.connect(self._add_selected)
        self.available_list.itemDoubleClicked.connect(
            lambda _item: self._add_selected()
        )
        move_up.clicked.connect(lambda: self._move_selected(-1))
        move_down.clicked.connect(lambda: self._move_selected(1))
        remove.clicked.connect(self._remove_selected)
        cancel.clicked.connect(self.reject)
        create.clicked.connect(self._create)

    def _add_selected(self) -> None:
        selected_rows = sorted(
            self.available_list.row(item)
            for item in self.available_list.selectedItems()
        )
        if not selected_rows:
            return
        known_hashes = {
            self.selected_list.item(row)
            .data(Qt.ItemDataRole.UserRole)[1]
            .content_hash
            for row in range(self.selected_list.count())
        }
        added = 0
        skipped_duplicate = 0
        skipped_limit = 0
        for row in selected_rows:
            if self.selected_list.count() >= MAX_REFERENCE_IMAGES:
                skipped_limit += 1
                continue
            role, image = self.available_list.item(row).data(
                Qt.ItemDataRole.UserRole
            )
            if image.content_hash in known_hashes:
                skipped_duplicate += 1
                continue
            item = QListWidgetItem(
                f"{ROLE_LABELS[role]}：{image.original_name}"
            )
            pixmap = load_scaled_pixmap(image.path, QSize(36, 36))
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            item.setData(Qt.ItemDataRole.UserRole, (role, image))
            item.setToolTip(f"{ROLE_LABELS[role]}：{image.original_name}")
            self.selected_list.addItem(item)
            known_hashes.add(image.content_hash)
            added += 1
        if self.selected_list.count():
            self.selected_list.setCurrentRow(self.selected_list.count() - 1)
        self._update_count()
        if skipped_duplicate or skipped_limit:
            messages = []
            if skipped_duplicate:
                messages.append(f"跳过 {skipped_duplicate} 张重复图片")
            if skipped_limit:
                messages.append(f"因 14 张上限忽略 {skipped_limit} 张")
            QMessageBox.information(
                self,
                "组合结果",
                f"已加入 {added} 张，" + "，".join(messages) + "。",
            )

    def _move_selected(self, offset: int) -> None:
        row = self.selected_list.currentRow()
        target = row + offset
        if not 0 <= row < self.selected_list.count():
            return
        if not 0 <= target < self.selected_list.count():
            return
        item = self.selected_list.takeItem(row)
        self.selected_list.insertItem(target, item)
        self.selected_list.setCurrentRow(target)

    def _remove_selected(self) -> None:
        row = self.selected_list.currentRow()
        if row >= 0:
            self.selected_list.takeItem(row)
            self._update_count()

    def _update_count(self) -> None:
        self.count_label.setText(
            f"已选择 {self.selected_list.count()} / {MAX_REFERENCE_IMAGES} 张"
        )

    def selected_items(self) -> list[tuple[str, StoredImage]]:
        return [
            self.selected_list.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.selected_list.count())
        ]

    def _create(self) -> None:
        if not self.selected_list.count():
            QMessageBox.information(self, "组合为空", "请至少加入一张图片。")
            return
        self.accept()


class DressPairCard(QFrame):
    remove_requested = Signal(object)
    skip_requested = Signal(object)
    selected = Signal(object)

    def __init__(
        self,
        pair: DressTaskGroup,
        position: int,
        parent: QWidget | None = None,
        *,
        allow_skip: bool = False,
    ) -> None:
        super().__init__(parent)
        self.pair = pair
        self._selected = False
        self.setObjectName("DressPairCard")
        self.setMinimumHeight(144)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QFrame#DressPairCard { background:#f8fafd; border:1px solid #dce5f1;"
            " border-radius:9px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 8)
        layout.setSpacing(5)

        header = QHBoxLayout()
        order = QLabel(f"{GROUP_KIND_LABELS.get(pair.kind, '任务组')} {position}")
        order.setStyleSheet("font-weight:700;color:#2e64c4;")
        remove = QPushButton("移除")
        remove.setObjectName("DangerButton")
        remove.setStyleSheet("padding: 3px 7px;")
        remove.clicked.connect(lambda: self.remove_requested.emit(self.pair))
        header.addWidget(order)
        header.addStretch(1)
        if allow_skip:
            skip = QPushButton(
                "恢复制作" if pair.skipped else "不做图"
            )
            skip.setStyleSheet("padding: 3px 7px;")
            skip.clicked.connect(
            lambda: self._skip_clicked()
            )
            header.addWidget(skip)
        header.addWidget(remove)
        layout.addLayout(header)

        reference_lines = [
            f"{index}. {ROLE_LABELS.get(role, role)}：{image.original_name}"
            for index, (role, image) in enumerate(
                zip(pair.roles, pair.references),
                start=1,
            )
        ]
        self.setToolTip("\n".join(reference_lines))
        if pair.skipped:
            self._apply_card_style()
        body = QHBoxLayout()
        body.setSpacing(5)
        body.addStretch(1)
        if pair.kind == "manual":
            body.addWidget(self._image_block(pair.garment, "服装"))
            arrow = QLabel("→")
            arrow.setStyleSheet("font-size:19px;color:#2e64c4;font-weight:700;")
            body.addWidget(arrow)
            body.addWidget(self._image_block(pair.person, "人物"))
        elif pair.kind == "auto":
            garments = tuple(
                image
                for role, image in zip(pair.roles, pair.references)
                if role == "garment"
            )
            body.addWidget(self._cluster_block(garments, "服装"))
            arrow = QLabel("→")
            arrow.setStyleSheet("font-size:19px;color:#2e64c4;font-weight:700;")
            body.addWidget(arrow)
            body.addWidget(self._image_block(pair.person, "人物"))
        else:
            body.addWidget(
                self._cluster_block(pair.references, "参考", pair.roles)
            )
        body.addStretch(1)
        layout.addLayout(body)

    def _skip_clicked(self) -> None:
        self.selected.emit(self.pair)
        self.skip_requested.emit(self.pair)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.pair)
        super().mouseReleaseEvent(event)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_card_style()

    def _apply_card_style(self) -> None:
        if self._selected:
            self.setStyleSheet(
                "QFrame#DressPairCard { background:#edf4ff;"
                "border:2px solid #3f73cb;border-radius:9px; }"
            )
        elif self.pair.skipped:
            self.setStyleSheet(
                "QFrame#DressPairCard { background:#f0f1f3;"
                "border:1px dashed #a9b1bd;border-radius:9px; }"
            )
        else:
            self.setStyleSheet(
                "QFrame#DressPairCard { background:#f8fafd;"
                "border:1px solid #dce5f1;border-radius:9px; }"
            )

    @staticmethod
    def _image_block(image: StoredImage, role_label: str) -> QWidget:
        block = QWidget()
        block.setFixedWidth(96)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        preview = QLabel()
        preview.setObjectName("DressPairThumbnail")
        preview.setFixedSize(68, 68)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = load_scaled_pixmap(image.path, QSize(66, 66))
        if not pixmap.isNull():
            preview.setPixmap(pixmap)
        display_name = (
            image.original_name
            if len(image.original_name) <= 12
            else f"{image.original_name[:6]}…{image.original_name[-4:]}"
        )
        name = QLabel(f"{role_label}：{display_name}")
        name.setObjectName("DressPairFileName")
        name.setFixedWidth(96)
        name.setWordWrap(True)
        name.setMaximumHeight(28)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setToolTip(image.original_name)
        name.setStyleSheet("color:#5d6b82;font-size:11px;")
        layout.addWidget(preview, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name)
        return block

    @staticmethod
    def _cluster_block(
        images: Sequence[StoredImage],
        label: str,
        roles: Sequence[str] | None = None,
    ) -> QWidget:
        block = QWidget()
        block.setFixedWidth(96)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        title_text = (
            f"参考×{len(images)}"
            if label == "参考"
            else f"{label}×{len(images)}"
        )
        title = QLabel(title_text)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color:#60708a;font-size:11px;font-weight:600;")
        thumbs = QHBoxLayout()
        thumbs.setSpacing(2)
        for index, image in enumerate(images[:3]):
            preview = QLabel()
            preview.setFixedSize(24, 48)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pixmap = load_scaled_pixmap(image.path, QSize(22, 44))
            if not pixmap.isNull():
                preview.setPixmap(pixmap)
            preview.setToolTip(
                f"{ROLE_LABELS.get(roles[index], roles[index]) if roles else label}"
                f"：{image.original_name}"
            )
            thumbs.addWidget(preview)
        if len(images) > 3:
            more = QLabel(f"+{len(images) - 3}")
            more.setFixedWidth(18)
            more.setAlignment(Qt.AlignmentFlag.AlignCenter)
            more.setStyleSheet(
                "color:#2e64c4;background:#e6eefe;border-radius:5px;"
                "padding:2px;font-weight:700;"
            )
            thumbs.addWidget(more)
        thumbs.addStretch(1)
        layout.addWidget(title)
        layout.addLayout(thumbs)
        return block


class BatchDressPage(QWidget):
    settings_requested = Signal()
    tasks_enqueued = Signal(int)
    templates_changed = Signal()

    def __init__(
        self,
        database: Database,
        file_store: FileStore,
        library_service: LibraryService,
        worker: TaskWorker,
        secret_store: SecretStore,
        parent: QWidget | None = None,
        *,
        external_top: QWidget | None = None,
        allow_pair_skip: bool = False,
        allow_bottom_skip: bool = False,
        generate_button_text: str = "生成全部配对并加入队列",
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.file_store = file_store
        self.library_service = library_service
        self.worker = worker
        self.secret_store = secret_store
        self.app_settings = AppSettings(database)
        self.garment_assets: list[StoredImage] = []
        self.person_assets: list[StoredImage] = []
        self.pairs: list[DressTaskGroup] = []
        self.selected_garment: StoredImage | None = None
        self.pair_columns = 4
        self._pair_layout_pending = False
        self._pair_panel_height = 0
        self.allow_pair_skip = allow_pair_skip
        self.allow_bottom_skip = allow_bottom_skip
        self.generate_button_text = generate_button_text
        self.selected_pair_key = None
        self.pair_cards: list[DressPairCard] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.form_content = content
        scroll.setWidget(content)
        self.page_scroll = scroll

        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 4, 4)
        layout.setSpacing(8)
        layout.addWidget(self._build_header())
        if external_top is not None:
            layout.addWidget(external_top)
        self.top_options_host = QWidget()
        top_options = QHBoxLayout(self.top_options_host)
        top_options.setContentsMargins(0, 0, 0, 0)
        top_options.setSpacing(12)
        self.settings_group = self._build_settings()
        self.prompt_group = self._build_prompt()
        top_options.addWidget(self.settings_group, 1)
        top_options.addWidget(self.prompt_group, 1)
        layout.addWidget(self.top_options_host)
        layout.addWidget(self._build_asset_columns(), 1)
        outer.addWidget(scroll, 5)
        self.pair_group = self._build_pairs()
        outer.addWidget(self.pair_group)
        self.submit_card = self._build_submit()
        outer.addWidget(self.submit_card)
        self.refresh_templates()
        QTimer.singleShot(0, self._update_pair_panel_height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_pair_panel_height)

    def _update_pair_panel_height(self) -> None:
        if not hasattr(self, "pair_group"):
            return
        page_height = self.height()
        target = 260 if page_height < 900 else max(260, round(page_height * 0.40))
        if target == self._pair_panel_height:
            return
        self._pair_panel_height = target
        self.pair_group.setFixedHeight(target)
        self.layout().activate()

    def _build_header(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        title = QLabel("批量换装")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel(
            "上传服装和人物素材，点击“服装 → 人物”或直接拖拽缩略图完成配对。"
        )
        subtitle.setStyleSheet("color:#6d7a91;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return card

    def _build_settings(self) -> QWidget:
        card = QGroupBox("生成设置")
        card.setObjectName("Card")
        form = QFormLayout(card)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.model_combo = QComboBox()
        for name, model_id in MODEL_SELECTION_OPTIONS:
            self.model_combo.addItem(name, model_id)
        self.model_combo.setCurrentIndex(
            next(
                index
                for index, (_, model_id) in enumerate(MODEL_SELECTION_OPTIONS)
                if model_id == DEFAULT_MODEL
            )
        )
        form.addRow("模型", self.model_combo)

        params = QWidget()
        param_layout = QHBoxLayout(params)
        param_layout.setContentsMargins(0, 0, 0, 0)
        self.aspect_combo = QComboBox()
        for value in ASPECT_RATIOS:
            self.aspect_combo.addItem(value, value)
        self.aspect_combo.setCurrentText(DEFAULT_ASPECT_RATIO)
        self.resolution_combo = QComboBox()
        for value in RESOLUTIONS:
            self.resolution_combo.addItem(
                "自动（不发送）" if value == "auto" else value,
                value,
            )
        self.resolution_combo.setCurrentIndex(
            next(
                index
                for index, value in enumerate(RESOLUTIONS)
                if value == DEFAULT_RESOLUTION
            )
        )
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 4)
        self.count_spin.setValue(DEFAULT_BATCH_SIZE)
        param_layout.addWidget(QLabel("宽高比"))
        param_layout.addWidget(self.aspect_combo)
        param_layout.addSpacing(8)
        param_layout.addWidget(QLabel("分辨率"))
        param_layout.addWidget(self.resolution_combo)
        param_layout.addSpacing(8)
        param_layout.addWidget(QLabel("每对数量"))
        param_layout.addWidget(self.count_spin)
        param_layout.addStretch(1)
        form.addRow("参数", params)
        self._additional_settings_rows(form)
        return card

    def _additional_settings_rows(self, _form: QFormLayout) -> None:
        return

    def _build_prompt(self) -> QWidget:
        card = QGroupBox("换装提示词")
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        template_row = QHBoxLayout()
        template_row.setSpacing(5)
        self._build_template_controls(template_row)
        layout.addLayout(template_row)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setMinimumHeight(96)
        self.prompt_edit.setMaximumHeight(110)
        self.prompt_edit.setPlaceholderText(
            "请描述换装要求。参考图顺序固定为：第 1 张是服装，第 2 张是人物。\n"
            "例如：将第 1 张图中的服装自然地穿到第 2 张图的人物身上，"
            "保持人物身份和姿态，准确还原服装颜色、材质与纹理。"
        )
        layout.addWidget(self.prompt_edit)
        return card

    def _build_template_controls(self, layout: QHBoxLayout) -> None:
        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(110)
        self.template_combo.addItem("选择提示词模板", None)
        save_template = QPushButton("保存当前")
        manage_templates = QPushButton("管理")
        for button in (save_template, manage_templates):
            button.setStyleSheet("padding: 5px 8px;")
        layout.addWidget(self.template_combo, 1)
        layout.addWidget(save_template)
        layout.addWidget(manage_templates)
        self.template_combo.currentIndexChanged.connect(self._apply_template)
        save_template.clicked.connect(self._quick_save_template)
        manage_templates.clicked.connect(self._manage_templates)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_templates()

    def refresh_templates(self) -> None:
        if not hasattr(self, "template_combo"):
            return
        selected = self.template_combo.currentData()
        selected_id = selected.get("id") if selected else None
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("选择提示词模板", None)
        for template in self.database.list_templates():
            self.template_combo.addItem(template["name"], template)
        if selected_id is not None:
            for index in range(self.template_combo.count()):
                data = self.template_combo.itemData(index)
                if data and data.get("id") == selected_id:
                    self.template_combo.setCurrentIndex(index)
                    break
        self.template_combo.blockSignals(False)

    def _apply_template(self, _index: int | None = None) -> None:
        template = self.template_combo.currentData()
        if template:
            self.prompt_edit.setPlainText(template["prompt"])

    def _quick_save_template(self) -> None:
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.information(self, "提示词为空", "请先填写提示词。")
            return
        name, ok = QInputDialog.getText(self, "保存提示词模板", "模板名称")
        if not ok or not name.strip():
            return
        try:
            self.database.save_template(name, prompt)
        except ValueError as exc:
            QMessageBox.warning(self, "无法保存", str(exc))
            return
        self.refresh_templates()
        self.templates_changed.emit()

    def _manage_templates(self) -> None:
        dialog = TemplateManagerDialog(
            self.database,
            current_prompt=self.prompt_edit.toPlainText(),
            parent=self,
        )
        dialog.exec()
        templates_changed = bool(dialog.templates_changed)
        if dialog.selected_prompt:
            self.prompt_edit.setPlainText(dialog.selected_prompt)
        self.refresh_templates()
        if templates_changed:
            self.templates_changed.emit()

    def _build_asset_columns(self) -> QWidget:
        host = QWidget()
        self.asset_columns_host = host
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        garment_group, self.garment_list = self._make_asset_group("garment")
        person_group, self.person_list = self._make_asset_group("person")
        self.garment_group = garment_group
        self.person_group = person_group
        layout.addWidget(garment_group, 1)
        layout.addWidget(person_group, 1)

        self.garment_list.currentItemChanged.connect(
            self._garment_selection_changed
        )
        self.person_list.currentItemChanged.connect(
            self._person_selection_changed
        )
        self.person_list.itemClicked.connect(self._person_clicked)
        self.garment_list.files_dropped.connect(
            lambda paths: self._append_assets("garment", paths)
        )
        self.person_list.files_dropped.connect(
            lambda paths: self._append_assets("person", paths)
        )
        self.person_list.garment_dropped.connect(self._garment_dropped)
        return host

    def _make_asset_group(self, role: str) -> tuple[QWidget, BatchAssetList]:
        group = BatchAssetDropGroup(
            f"{ROLE_LABELS[role]}素材（最多 {MAX_BATCH_ASSETS} 张）"
        )
        group.setObjectName("Card")
        group.setAcceptDrops(True)
        layout = QVBoxLayout(group)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        add_images = QPushButton("添加图片")
        add_folder = QPushButton("添加文件夹")
        add_library = QPushButton("作品库")
        crop = QPushButton("裁剪")
        restore = QPushButton("恢复")
        remove = QPushButton("移除所选")
        clear = QPushButton("清空")
        add_library.setToolTip("从作品库添加")
        for button in (
            add_images,
            add_folder,
            add_library,
            crop,
            restore,
            remove,
            clear,
        ):
            button.setStyleSheet("padding: 6px 8px;")
        buttons.addWidget(add_images)
        buttons.addWidget(add_folder)
        buttons.addWidget(add_library)
        buttons.addWidget(crop)
        buttons.addWidget(restore)
        buttons.addStretch(1)
        buttons.addWidget(remove)
        buttons.addWidget(clear)
        layout.addLayout(buttons)

        asset_list = BatchAssetList(role)
        layout.addWidget(asset_list, 1)
        count_label = QLabel(f"已选择 0 / {MAX_BATCH_ASSETS} 张")
        count_label.setStyleSheet("color:#6d7a91;")
        layout.addWidget(count_label)
        if role == "garment":
            self.garment_count_label = count_label
            self.garment_crop_button = crop
            self.garment_restore_button = restore
        else:
            self.person_count_label = count_label
            self.person_crop_button = crop
            self.person_restore_button = restore

        add_images.clicked.connect(lambda: self._add_image_files(role))
        add_folder.clicked.connect(lambda: self._add_image_folder(role))
        add_library.clicked.connect(lambda: self._add_library_images(role))
        crop.clicked.connect(lambda: self._crop_selected(role))
        restore.clicked.connect(lambda: self._restore_selected(role))
        remove.clicked.connect(lambda: self._remove_selected(role))
        clear.clicked.connect(lambda: self._clear_role(role))
        group.files_dropped.connect(
            lambda paths: self._append_assets(role, paths)
        )
        crop.setEnabled(False)
        restore.setEnabled(False)
        return group, asset_list

    def _build_pairs(self) -> QWidget:
        group = QGroupBox("配对任务")
        group.setObjectName("Card")
        layout = QVBoxLayout(group)
        top = QHBoxLayout()
        self.pair_count_label = QLabel("已建立 0 个配对")
        self.pair_count_label.setStyleSheet("color:#2e64c4;font-weight:600;")
        self.free_compose_button = QPushButton("自由组合")
        self.auto_match_button = QPushButton("自动匹配")
        clear_pairs = QPushButton("清空配对")
        top.addWidget(self.pair_count_label)
        top.addStretch(1)
        top.addWidget(self.free_compose_button)
        top.addWidget(self.auto_match_button)
        top.addWidget(clear_pairs)
        layout.addLayout(top)

        self.pair_scroll = QScrollArea()
        self.pair_scroll.setWidgetResizable(True)
        self.pair_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.pair_scroll.setMinimumHeight(200)
        self.pair_host = QWidget()
        self.pair_grid = QGridLayout(self.pair_host)
        self.pair_grid.setContentsMargins(0, 0, 0, 0)
        self.pair_grid.setVerticalSpacing(8)
        self.pair_grid.setHorizontalSpacing(8)
        self.pair_grid.setSpacing(8)
        self.pair_scroll.setWidget(self.pair_host)
        self.pair_scroll.viewport().installEventFilter(self)
        layout.addWidget(self.pair_scroll, 1)
        group.setMinimumHeight(260)
        group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.free_compose_button.clicked.connect(self._free_compose)
        self.auto_match_button.clicked.connect(self._auto_match)
        clear_pairs.clicked.connect(self._clear_pairs)
        self._refresh_pairs()
        return group

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "pair_scroll", None) or (
            hasattr(self, "pair_scroll")
            and watched is self.pair_scroll.viewport()
        ):
            if event.type() == QEvent.Type.Resize:
                self._schedule_pair_layout()
        return super().eventFilter(watched, event)

    def _schedule_pair_layout(self) -> None:
        if self._pair_layout_pending:
            return
        self._pair_layout_pending = True
        QTimer.singleShot(0, self._update_pair_columns)

    def _update_pair_columns(self) -> None:
        self._pair_layout_pending = False
        width = self.pair_scroll.viewport().width()
        columns = max(
            MIN_PAIR_COLUMNS,
            min(
                MAX_PAIR_COLUMNS,
                (max(1, width) + PAIR_CARD_GAP)
                // (PAIR_CARD_MIN_WIDTH + PAIR_CARD_GAP),
            ),
        )
        if columns != self.pair_columns:
            self.pair_columns = columns
            self._refresh_pairs()

    def _build_submit(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        layout = QHBoxLayout(card)
        self.feedback_label = QLabel(
            "先选择服装，再点击人物；也可把服装缩略图直接拖到人物上。"
        )
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setStyleSheet("color:#6d7a91;")
        self.bottom_skip_button = QPushButton("不做图")
        self.bottom_skip_button.setObjectName("DangerButton")
        self.bottom_skip_button.clicked.connect(
            self._toggle_selected_pair_skip
        )
        self.bottom_skip_button.setEnabled(self.allow_bottom_skip)
        self.bottom_skip_button.setVisible(self.allow_bottom_skip)
        self.generate_button = QPushButton(self.generate_button_text)
        self.generate_button.setObjectName("PrimaryButton")
        self.generate_button.setMinimumHeight(42)
        self.generate_button.clicked.connect(self._enqueue_all)
        layout.addWidget(self.feedback_label, 1)
        layout.addWidget(self.bottom_skip_button)
        layout.addWidget(self.generate_button)
        return card

    def _assets_for(self, role: str) -> list[StoredImage]:
        return self.garment_assets if role == "garment" else self.person_assets

    def _asset_list_for(self, role: str) -> BatchAssetList:
        return self.garment_list if role == "garment" else self.person_list

    def _refresh_role(self, role: str) -> None:
        assets = self._assets_for(role)
        self._asset_list_for(role).refresh_assets(assets)
        count_label = (
            self.garment_count_label
            if role == "garment"
            else self.person_count_label
        )
        count_label.setText(f"已选择 {len(assets)} / {MAX_BATCH_ASSETS} 张")
        if role == "garment" and self.selected_garment not in assets:
            self.selected_garment = None
        self._update_asset_actions()

    def _update_asset_actions(self) -> None:
        for role in ("garment", "person"):
            asset_list = self._asset_list_for(role)
            assets = self._assets_for(role)
            row = asset_list.currentRow()
            has_selection = 0 <= row < len(assets)
            crop_button = (
                self.garment_crop_button
                if role == "garment"
                else self.person_crop_button
            )
            restore_button = (
                self.garment_restore_button
                if role == "garment"
                else self.person_restore_button
            )
            image = assets[row] if has_selection else None
            crop_button.setEnabled(has_selection)
            restore_button.setEnabled(
                bool(image and image.source_path and image.source_path.is_file())
            )

    def _selected_asset(self, role: str) -> tuple[int, StoredImage] | None:
        row = self._asset_list_for(role).currentRow()
        assets = self._assets_for(role)
        if not 0 <= row < len(assets):
            return None
        return row, assets[row]

    def _crop_selected(self, role: str) -> None:
        selected = self._selected_asset(role)
        if selected is None:
            return
        row, image = selected
        dialog = CropDialog(image.path, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        crop_box = dialog.crop_box()
        if crop_box is None:
            return
        try:
            candidate = self.file_store.save_cropped_reference(image, crop_box)
        except Exception as exc:
            QMessageBox.warning(self, "裁剪失败", str(exc))
            return
        self._replace_asset(role, row, candidate, action_label="裁剪")

    def _restore_selected(self, role: str) -> None:
        selected = self._selected_asset(role)
        if selected is None:
            return
        row, image = selected
        if not image.source_path or not image.source_path.is_file():
            QMessageBox.information(
                self,
                "无法恢复",
                "原始素材文件已不存在。",
            )
            self._update_asset_actions()
            return
        try:
            candidate = self.file_store.save_reference(image.source_path)
        except Exception as exc:
            QMessageBox.warning(self, "恢复失败", str(exc))
            return
        self._replace_asset(role, row, candidate, action_label="恢复")

    def _replace_asset(
        self,
        role: str,
        row: int,
        candidate: StoredImage,
        *,
        action_label: str,
    ) -> bool:
        assets = self._assets_for(role)
        if not 0 <= row < len(assets):
            return False
        if any(
            index != row and image.content_hash == candidate.content_hash
            for index, image in enumerate(assets)
        ):
            QMessageBox.information(
                self,
                "同列已有相同图片",
                f"该{ROLE_LABELS[role]}图片与当前列中的另一张图片内容相同，"
                "已取消替换。",
            )
            return False

        previous = assets[row]
        assets[row] = candidate
        if role == "garment" and self.selected_garment == previous:
            self.selected_garment = candidate
        self.pairs = [
            DressTaskGroup(
                references=tuple(
                    candidate
                    if reference == previous and reference_role == role
                    else reference
                    for reference_role, reference in zip(pair.roles, pair.references)
                ),
                roles=pair.roles,
                kind=pair.kind,
                auto_prompt=pair.auto_prompt,
                skipped=pair.skipped,
            )
            if any(
                reference_role == role and reference == previous
                for reference_role, reference in zip(pair.roles, pair.references)
            )
            else pair
            for pair in self.pairs
        ]
        self._refresh_role(role)
        self._asset_list_for(role).setCurrentRow(row)
        self._refresh_pairs()
        self.feedback_label.setText(
            f"{ROLE_LABELS[role]} {row + 1} 已{action_label}，相关配对已同步更新。"
        )
        return True

    def _append_assets(
        self,
        role: str,
        inputs: Sequence[Path],
        *,
        remember_path: bool = True,
    ) -> None:
        if remember_path and inputs:
            first = Path(inputs[0])
            folder = first if first.is_dir() else first.parent
            self.app_settings.set_input_folder(
                INPUT_BATCH_GARMENT if role == "garment" else INPUT_BATCH_PERSON,
                folder,
            )
        assets = self._assets_for(role)
        remaining = MAX_BATCH_ASSETS - len(assets)
        if remaining <= 0:
            QMessageBox.information(
                self,
                "已达到上限",
                f"{ROLE_LABELS[role]}素材最多 {MAX_BATCH_ASSETS} 张。",
            )
            return

        paths, scan_errors = scan_image_inputs(inputs)
        known_hashes = {image.content_hash for image in assets}
        duplicate_count = 0
        invalid_messages = list(scan_errors)
        added = 0
        overflow = 0
        for index, path in enumerate(paths):
            if len(assets) >= MAX_BATCH_ASSETS:
                overflow = len(paths) - index
                break
            try:
                image = self.file_store.save_reference(path)
            except (OSError, ImageValidationError) as exc:
                invalid_messages.append(f"{path.name}：{exc}")
                continue
            if image.content_hash in known_hashes:
                duplicate_count += 1
                continue
            assets.append(image)
            known_hashes.add(image.content_hash)
            added += 1

        self._refresh_role(role)
        summary = [f"已添加 {added} 张{ROLE_LABELS[role]}图片"]
        if duplicate_count:
            summary.append(f"跳过 {duplicate_count} 张重复图片")
        if overflow:
            summary.append(f"因上限未导入 {overflow} 张")
        message = "，".join(summary) + "。"
        self.feedback_label.setText(message)
        if invalid_messages:
            QMessageBox.warning(
                self,
                "部分图片未导入",
                message + "\n\n" + "\n".join(invalid_messages[:8]),
            )
        elif duplicate_count or overflow:
            QMessageBox.information(self, "导入结果", message)

    def _add_image_files(self, role: str) -> None:
        remaining = MAX_BATCH_ASSETS - len(self._assets_for(role))
        if remaining <= 0:
            QMessageBox.information(
                self,
                "已达到上限",
                f"{ROLE_LABELS[role]}素材最多 {MAX_BATCH_ASSETS} 张。",
            )
            return
        scope = INPUT_BATCH_GARMENT if role == "garment" else INPUT_BATCH_PERSON
        initial = self.app_settings.input_folder(scope)
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            f"选择{ROLE_LABELS[role]}图片",
            str(initial) if initial else "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;所有文件 (*)",
        )
        if paths:
            self._append_assets(role, [Path(path) for path in paths])

    def _add_image_folder(self, role: str) -> None:
        scope = INPUT_BATCH_GARMENT if role == "garment" else INPUT_BATCH_PERSON
        initial = self.app_settings.input_folder(scope)
        directory = QFileDialog.getExistingDirectory(
            self,
            f"选择{ROLE_LABELS[role]}文件夹",
            str(initial) if initial else "",
        )
        if directory:
            self._append_assets(role, [Path(directory)])

    def _add_library_images(self, role: str) -> None:
        remaining = MAX_BATCH_ASSETS - len(self._assets_for(role))
        if remaining <= 0:
            QMessageBox.information(
                self,
                "已达到上限",
                f"{ROLE_LABELS[role]}素材最多 {MAX_BATCH_ASSETS} 张。",
            )
            return
        dialog = LibraryPickerDialog(self.database, remaining, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._append_assets(
            role,
            dialog.selected_paths(),
            remember_path=False,
        )

    def _remove_selected(self, role: str) -> None:
        asset_list = self._asset_list_for(role)
        row = asset_list.currentRow()
        assets = self._assets_for(role)
        if not 0 <= row < len(assets):
            return
        image = assets[row]
        related = self._pairs_for_image(role, image)
        if related and not self._confirm_pair_removal(
            f"该{ROLE_LABELS[role]}图已参与 {len(related)} 个配对，"
            "移除后将同时删除这些配对。"
        ):
            return
        assets.pop(row)
        self._remove_related_pairs(role, image)
        self._refresh_role(role)
        self._refresh_pairs()

    def _clear_role(self, role: str) -> None:
        assets = self._assets_for(role)
        if not assets:
            return
        asset_hashes = {asset.content_hash for asset in assets}
        related_count = sum(
            1
            for pair in self.pairs
            if any(
                reference_role == role and reference.content_hash in asset_hashes
                for reference_role, reference in zip(pair.roles, pair.references)
            )
        )
        if related_count and not self._confirm_pair_removal(
            f"清空后将同时删除 {related_count} 个相关配对。"
        ):
            return
        assets.clear()
        self.pairs = [
            pair
            for pair in self.pairs
            if not any(
                reference_role == role
                and reference.content_hash in asset_hashes
                for reference_role, reference in zip(pair.roles, pair.references)
            )
        ]
        if role == "garment":
            self.selected_garment = None
        self._refresh_role(role)
        self._refresh_pairs()

    def _confirm_pair_removal(self, detail: str) -> bool:
        answer = QMessageBox.question(
            self,
            "移除已配对素材",
            detail + "\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _pairs_for_image(
        self,
        role: str,
        image: StoredImage,
    ) -> list[DressTaskGroup]:
        return [
            pair
            for pair in self.pairs
            if any(
                reference_role == role and reference == image
                for reference_role, reference in zip(pair.roles, pair.references)
            )
        ]

    def _remove_related_pairs(self, role: str, image: StoredImage) -> None:
        self.pairs = [
            pair
            for pair in self.pairs
            if not any(
                reference_role == role and reference == image
                for reference_role, reference in zip(pair.roles, pair.references)
            )
        ]

    def _garment_selection_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        del previous
        image = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.selected_garment = image if isinstance(image, StoredImage) else None
        self._update_asset_actions()
        if self.selected_garment:
            self.feedback_label.setText(
                f"已选择服装：{self.selected_garment.original_name}，请点击右侧人物图片。"
            )

    def _person_selection_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        del current, previous
        self._update_asset_actions()

    def _person_clicked(self, item: QListWidgetItem) -> None:
        image = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(image, StoredImage):
            return
        if self.selected_garment is None:
            self.feedback_label.setText("请先点击一张服装图片，再点击人物图片。")
            return
        self._add_pair(self.selected_garment, image)

    def _garment_dropped(self, content_hash: str, person_row: int) -> None:
        garment = next(
            (
                image
                for image in self.garment_assets
                if image.content_hash == content_hash
            ),
            None,
        )
        if garment is None or not 0 <= person_row < len(self.person_assets):
            return
        self._add_pair(garment, self.person_assets[person_row])

    def _free_compose(self) -> None:
        if not self.garment_assets and not self.person_assets:
            QMessageBox.information(
                self,
                "没有可用素材",
                "请先上传服装或人物素材。",
            )
            return
        dialog = FreeComposeDialog(
            self.garment_assets,
            self.person_assets,
            self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        selected = dialog.selected_items()
        if not selected:
            return
        group = DressTaskGroup(
            references=tuple(image for _, image in selected),
            roles=tuple(role for role, _ in selected),
            kind="free",
            auto_prompt=True,
        )
        if any(existing.key == group.key for existing in self.pairs):
            self.feedback_label.setText("完全相同的自由组合已经存在。")
            return
        self.pairs.append(group)
        self.feedback_label.setText(
            f"已添加自由组合，共 {len(group.references)} 张参考图。"
        )
        self._refresh_pairs()

    def _auto_match(self) -> None:
        if not self.garment_assets:
            QMessageBox.information(
                self,
                "缺少服装素材",
                "请先上传至少一张服装图片。",
            )
            return
        if not self.person_assets:
            QMessageBox.information(
                self,
                "缺少人物素材",
                "请先上传至少一张人物图片。",
            )
            return
        if self.pairs:
            answer = QMessageBox.question(
                self,
                "替换现有配对",
                f"自动匹配将替换当前 {len(self.pairs)} 个配对，"
                "已上传素材不会删除。\n确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        garment_chunk_size = MAX_REFERENCE_IMAGES - 1
        groups: list[DressTaskGroup] = []
        for person in self.person_assets:
            for start in range(0, len(self.garment_assets), garment_chunk_size):
                garments = tuple(
                    self.garment_assets[start : start + garment_chunk_size]
                )
                groups.append(
                    DressTaskGroup(
                        references=(*garments, person),
                        roles=("garment",) * len(garments) + ("person",),
                        kind="auto",
                        auto_prompt=True,
                    )
                )
        if not groups:
            return
        self.pairs = groups
        self._reset_pair_selection()
        self._refresh_pairs()
        split_count = (len(self.garment_assets) + garment_chunk_size - 1) // garment_chunk_size
        split_hint = f"，每位人物拆为 {split_count} 组" if split_count > 1 else ""
        self.feedback_label.setText(
            f"自动匹配已生成 {len(groups)} 组{split_hint}。"
        )

    def _add_pair(self, garment: StoredImage, person: StoredImage) -> None:
        pair = DressTaskGroup.manual(garment, person)
        if any(existing.key == pair.key for existing in self.pairs):
            self.feedback_label.setText("这组“服装 × 人物”已经配对，无需重复添加。")
            self._reset_pair_selection()
            return
        self.pairs.append(pair)
        self.feedback_label.setText(
            f"已建立配对 {len(self.pairs)}："
            f"{garment.original_name} × {person.original_name}"
        )
        self._reset_pair_selection()
        self._refresh_pairs()

    def _reset_pair_selection(self) -> None:
        self.selected_garment = None
        self.garment_list.blockSignals(True)
        self.person_list.blockSignals(True)
        self.garment_list.clearSelection()
        self.person_list.clearSelection()
        self.garment_list.setCurrentRow(-1)
        self.person_list.setCurrentRow(-1)
        self.garment_list.blockSignals(False)
        self.person_list.blockSignals(False)
        self._update_asset_actions()

    def _remove_pair(self, pair: DressTaskGroup) -> None:
        self.pairs = [item for item in self.pairs if item.key != pair.key]
        self._refresh_pairs()

    def _toggle_pair_skip(self, pair: DressTaskGroup) -> None:
        self.selected_pair_key = pair.key
        self.pairs = [
            replace(item, skipped=not item.skipped)
            if item.key == pair.key
            else item
            for item in self.pairs
        ]
        self._refresh_pairs()

    def _select_pair(self, pair: DressTaskGroup) -> None:
        if not self.allow_pair_skip:
            return
        self.selected_pair_key = pair.key
        for card in self.pair_cards:
            card.set_selected(card.pair.key == pair.key)
        self._update_bottom_skip_button()

    def _toggle_selected_pair_skip(self) -> None:
        pair = next(
            (
                item
                for item in self.pairs
                if item.key == self.selected_pair_key
            ),
            None,
        )
        if pair is None:
            QMessageBox.information(
                self,
                "未选择配对",
                "请先选择一张配对卡。",
            )
            return
        self._toggle_pair_skip(pair)

    def _update_bottom_skip_button(self) -> None:
        if not hasattr(self, "bottom_skip_button"):
            return
        pair = next(
            (
                item
                for item in self.pairs
                if item.key == self.selected_pair_key
            ),
            None,
        )
        self.bottom_skip_button.setEnabled(self.allow_bottom_skip)
        self.bottom_skip_button.setText(
            "恢复制作" if pair is not None and pair.skipped else "不做图"
        )

    def _clear_pairs(self) -> None:
        if not self.pairs:
            return
        self.pairs.clear()
        self._reset_pair_selection()
        self._refresh_pairs()
        self.feedback_label.setText("配对已清空，素材仍保留。")

    def _refresh_pairs(self) -> None:
        scrollbar = self.pair_scroll.verticalScrollBar()
        scroll_value = scrollbar.value()
        while self.pair_grid.count():
            item = self.pair_grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        self.pair_count_label.setText(f"已建立 {len(self.pairs)} 个配对")
        for column in range(MAX_PAIR_COLUMNS):
            self.pair_grid.setColumnStretch(column, 0)
        if not self.pairs:
            self.selected_pair_key = None
            self.pair_cards.clear()
            self._update_bottom_skip_button()
            empty = QLabel(
                "还没有配对。选择服装后点击人物，或把左侧服装缩略图拖到右侧人物上。"
            )
            empty.setWordWrap(True)
            empty.setStyleSheet("color:#7d8797;padding:24px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.pair_grid.addWidget(empty, 0, 0, 1, self.pair_columns)
            for column in range(self.pair_columns):
                self.pair_grid.setColumnStretch(column, 1)
            return
        if not any(
            pair.key == self.selected_pair_key for pair in self.pairs
        ):
            self.selected_pair_key = None
        self.pair_cards.clear()
        for index, pair in enumerate(self.pairs):
            card = DressPairCard(
                pair,
                index + 1,
                allow_skip=self.allow_pair_skip,
            )
            card.remove_requested.connect(self._remove_pair)
            card.skip_requested.connect(self._toggle_pair_skip)
            card.selected.connect(self._select_pair)
            card.set_selected(pair.key == self.selected_pair_key)
            self.pair_cards.append(card)
            self.pair_grid.addWidget(
                card,
                index // self.pair_columns,
                index % self.pair_columns,
            )
        for column in range(self.pair_columns):
            self.pair_grid.setColumnStretch(column, 1)
        QTimer.singleShot(
            0,
            lambda: scrollbar.setValue(min(scroll_value, scrollbar.maximum())),
        )
        self._update_bottom_skip_button()

    def _enqueue_all(self) -> None:
        if not self.secret_store.load():
            QMessageBox.warning(
                self,
                "API Key 不可用",
                "当前账号尚未配置 API Key，请联系管理员。",
            )
            return
        if not self.pairs:
            QMessageBox.information(self, "没有配对", "请至少建立一组服装与人物配对。")
            return
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.information(self, "缺少提示词", "请先填写换装提示词。")
            return

        count = self.count_spin.value()
        maximum = len(self.pairs) * count
        answer = QMessageBox.question(
            self,
            "确认批量生成",
            f"将创建 {len(self.pairs)} 个排队任务，每对最多生成 {count} 张，"
            f"本批最多 {maximum} 张图片。\n"
            "任务会串行执行并可能产生费用，失败后不会自动重试。\n"
            "确定加入队列吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        requests = []
        selected_model = self.model_combo.currentData()
        strategy = self.app_settings.auto_strategy()
        for group in self.pairs:
            prompt_for_group = self._prompt_for_group(prompt, group)
            task_model = selected_model
            auto_strategy = None
            auto_reason = None
            mode = MODE_FREE_COMPOSE if group.kind == "free" else MODE_DRESS_UP
            if selected_model == AUTO_MODEL_ID:
                decision = choose_model(
                    prompt=prompt_for_group,
                    mode=mode,
                    reference_count=len(group.references),
                    strategy=strategy,
                )
                task_model = decision.model
                auto_strategy = decision.strategy
                auto_reason = decision.reason
            requests.append(
                ImageRequest(
                    model=task_model,
                    prompt=prompt_for_group,
                    n=count,
                    aspect_ratio=self.aspect_combo.currentData(),
                    resolution=self.resolution_combo.currentData(),
                    reference_images=group.references,
                    mode=mode,
                    auto_strategy=auto_strategy,
                    auto_reason=auto_reason,
                )
            )
        try:
            task_ids = self.database.create_tasks(requests)
        except Exception as exc:
            QMessageBox.critical(self, "无法创建任务", str(exc))
            return

        self.pairs.clear()
        self._reset_pair_selection()
        self._refresh_pairs()
        self.worker.notify()
        self.tasks_enqueued.emit(len(task_ids))
        self.feedback_label.setText(
            f"已加入 {len(task_ids)} 个排队任务，"
            f"任务编号 #{task_ids[0]}–#{task_ids[-1]}。"
        )
        QMessageBox.information(
            self,
            "已加入队列",
            f"已加入 {len(task_ids)} 个任务。配对已清空，素材仍保留。\n"
            "可在“创作”的任务队列或“历史记录”中查看结果。",
        )

    @staticmethod
    def _prompt_for_group(user_prompt: str, group: DressTaskGroup) -> str:
        if not group.auto_prompt:
            return user_prompt
        if group.kind == "auto":
            garment_count = group.garment_count
            if garment_count == 1:
                context = (
                    "自动匹配：第 1 张为服装参考，第 2 张为人物参考。"
                )
            else:
                context = (
                    f"自动匹配：第 1 至第 {garment_count} 张为服装参考，"
                    f"第 {garment_count + 1} 张为人物参考。"
                )
        else:
            descriptions = "，".join(
                f"第 {index} 张为{ROLE_LABELS.get(role, role)}素材"
                for index, role in enumerate(group.roles, start=1)
            )
            context = f"自由组合：{descriptions}。"
        return f"{context}\n{user_prompt}"

    def prepare_retry(self, task_id: int) -> None:
        task = self.database.get_task(task_id)
        if task is None or task["mode"] not in (MODE_DRESS_UP, MODE_FREE_COMPOSE):
            return
        references = self.database.get_task_references(task_id)
        if not references:
            return
        user_prompt, kind, roles = self._retry_group_data(
            task["mode"],
            task["prompt"],
            len(references),
        )
        self.garment_assets = [
            image
            for role, image in zip(roles, references)
            if role == "garment"
        ]
        self.person_assets = [
            image
            for role, image in zip(roles, references)
            if role == "person"
        ]
        self.pairs = [
            DressTaskGroup(
                references=tuple(references),
                roles=roles,
                kind=kind,
                auto_prompt=kind in ("auto", "free"),
            )
        ]
        self.selected_garment = None
        self._refresh_role("garment")
        self._refresh_role("person")
        self._refresh_pairs()
        self.prompt_edit.setPlainText(user_prompt)
        model_index = self.model_combo.findData(task["model"])
        if model_index >= 0:
            self.model_combo.setCurrentIndex(model_index)
        aspect_index = self.aspect_combo.findData(task["aspect_ratio"])
        if aspect_index >= 0:
            self.aspect_combo.setCurrentIndex(aspect_index)
        resolution_index = self.resolution_combo.findData(task["resolution"] or "auto")
        if resolution_index >= 0:
            self.resolution_combo.setCurrentIndex(resolution_index)
        self.count_spin.setValue(int(task["n"]))
        self.feedback_label.setText(
            f"已载入任务 #{task_id}，修改提示词或参数后重新生成。"
        )
        self.prompt_edit.setFocus()

    @staticmethod
    def _retry_group_data(
        mode: str,
        prompt: str,
        reference_count: int,
    ) -> tuple[str, str, tuple[str, ...]]:
        first_line, separator, remainder = prompt.partition("\n")
        if mode == MODE_FREE_COMPOSE and first_line.startswith("自由组合："):
            matches = re.findall(
                r"第 (\d+) 张为(服装|人物)素材",
                first_line,
            )
            if len(matches) == reference_count and separator:
                roles = tuple(
                    "garment" if label == "服装" else "person"
                    for _, label in matches
                )
                return remainder, "free", roles
            return prompt, "free", ("garment",) * reference_count

        if mode == MODE_DRESS_UP and first_line.startswith("自动匹配："):
            match = re.match(
                r"自动匹配：第 1 至第 (\d+) 张为服装参考，第 (\d+) 张为人物参考。",
                first_line,
            )
            single_match = re.match(
                r"自动匹配：第 1 张为服装参考，第 2 张为人物参考。",
                first_line,
            )
            if separator and match:
                garment_count = int(match.group(1))
                if garment_count + 1 == reference_count:
                    return (
                        remainder,
                        "auto",
                        ("garment",) * garment_count + ("person",),
                    )
            if separator and single_match and reference_count == 2:
                return remainder, "auto", ("garment", "person")

        if reference_count == 2:
            return prompt, "manual", ("garment", "person")
        return (
            prompt,
            "auto",
            ("garment",) * (reference_count - 1) + ("person",),
        )
