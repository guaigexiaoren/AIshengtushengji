from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
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
    MODE_DRESS_UP,
    MODE_FREE_COMPOSE,
    MODE_LABELS,
    MODE_REFERENCE,
    MODE_TEXT,
    MODEL_LABELS,
    MODEL_SELECTION_OPTIONS,
    RESOLUTIONS,
    STATUS_LABELS,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCESS,
)
from ..database import Database
from ..library import LibraryService
from ..local_export import LocalImageExporter
from ..model_router import choose_model
from ..models import ImageRequest, StoredImage
from ..security import SecretStore
from ..settings import (
    AUTO_STRATEGY_LABELS,
    AppSettings,
    INPUT_CREATE_REFERENCE,
)
from ..storage import FileStore, ImageValidationError
from ..time_utils import format_task_elapsed
from ..worker import TaskWorker
from .dialogs import CompareDialog, LibraryPickerDialog, TemplateManagerDialog
from .crop import CropDialog
from .save_helpers import LocalSaveController, show_saved_path, show_saved_toast
from .widgets import (
    CompareWidget,
    ImageStrip,
    ImageView,
    ReferenceDropGroup,
    ReorderableImageStrip,
    TaskImageAsset,
    build_task_assets,
    scan_image_inputs,
)

TASK_DATA_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class CreatePage(QWidget):
    settings_requested = Signal()
    library_changed = Signal()
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
        auto_load: bool = True,
        async_thumbnails: bool = False,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.file_store = file_store
        self.library_service = library_service
        self.worker = worker
        self.secret_store = secret_store
        self.async_thumbnails = async_thumbnails
        self.app_settings = AppSettings(database)
        self.local_save_controller = LocalSaveController(
            self,
            LocalImageExporter(database, self.app_settings),
            self.app_settings,
        )
        self.reference_images: list[StoredImage] = []
        self.current_task_id: int | None = None
        self.current_results: list[dict] = []
        self.current_assets: list[TaskImageAsset] = []
        self.current_task_snapshot: dict | None = None
        self._updating_queue = False

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(splitter)

        left_host = QWidget()
        left_layout = QVBoxLayout(left_host)
        left_layout.setContentsMargins(0, 0, 10, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(self._build_form(), 1)

        right_host = QWidget()
        right_layout = QVBoxLayout(right_host)
        right_layout.setContentsMargins(10, 0, 0, 0)
        right_layout.addWidget(self._build_preview(), 1)

        splitter.addWidget(left_host)
        splitter.addWidget(right_host)
        splitter.setSizes([580, 700])

        self.worker.signals.task_changed.connect(self._on_task_changed)
        self.mode_combo.currentIndexChanged.connect(self._update_reference_visibility)
        self.reference_strip.currentRowChanged.connect(self._preview_reference)
        if auto_load:
            self.refresh_templates()
            self.refresh_queue()
        self._update_reference_list()
        self._update_reference_visibility()
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self._update_elapsed_displays)
        self.elapsed_timer.start()

    def _build_form(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.form_content = content
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 4, 4)
        layout.setSpacing(12)

        header = QFrame()
        header.setObjectName("Card")
        header_layout = QVBoxLayout(header)
        title = QLabel("开始创作")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel("选择 Nano Banana 型号，输入提示词后加入串行任务队列。")
        subtitle.setStyleSheet("color:#6d7a91;")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        layout.addWidget(header)

        settings_card = QGroupBox("生成设置")
        settings_card.setObjectName("Card")
        form = QFormLayout(settings_card)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)

        self.model_combo = QComboBox()
        for name, model_id in MODEL_SELECTION_OPTIONS:
            self.model_combo.addItem(name, model_id)
        self.model_combo.setCurrentIndex(
            next(
                i
                for i, (_, model_id) in enumerate(MODEL_SELECTION_OPTIONS)
                if model_id == DEFAULT_MODEL
            )
        )
        form.addRow("模型", self.model_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(MODE_LABELS[MODE_TEXT], MODE_TEXT)
        self.mode_combo.addItem(MODE_LABELS[MODE_REFERENCE], MODE_REFERENCE)
        self.mode_combo.setCurrentIndex(
            self.mode_combo.findData(MODE_REFERENCE)
        )
        form.addRow("模式", self.mode_combo)

        template_row = QWidget()
        template_layout = QHBoxLayout(template_row)
        template_layout.setContentsMargins(0, 0, 0, 0)
        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(120)
        self.template_combo.addItem("选择提示词模板", None)
        save_template = QPushButton("保存当前")
        manage_template = QPushButton("管理")
        template_layout.addWidget(self.template_combo, 1)
        template_layout.addWidget(save_template)
        template_layout.addWidget(manage_template)
        form.addRow("模板", template_row)

        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText(
            "描述画面主体、风格、构图、光线和需要保留或修改的细节。\n"
            "使用多张参考图时，可在提示词中按“第 1 张、第 2 张”指定用途。"
        )
        self.prompt_edit.setMinimumHeight(150)
        form.addRow("提示词", self.prompt_edit)

        self.reference_group = ReferenceDropGroup("参考图（最多 14 张）")
        self.reference_group.setObjectName("Card")
        reference_layout = QVBoxLayout(self.reference_group)
        reference_buttons = QHBoxLayout()
        reference_buttons.setSpacing(4)
        add_local = QPushButton("添加图片")
        add_folder = QPushButton("添加文件夹")
        add_library = QPushButton("从作品库添加")
        self.crop_reference_button = QPushButton("裁剪")
        self.restore_reference_button = QPushButton("恢复")
        remove_selected = QPushButton("移除所选")
        clear_all = QPushButton("清空")
        for button in (
            add_local,
            add_folder,
            add_library,
            self.crop_reference_button,
            self.restore_reference_button,
            remove_selected,
            clear_all,
        ):
            button.setStyleSheet("padding: 6px 8px;")
        reference_buttons.addWidget(add_local)
        reference_buttons.addWidget(add_folder)
        reference_buttons.addWidget(add_library)
        reference_buttons.addWidget(self.crop_reference_button)
        reference_buttons.addWidget(self.restore_reference_button)
        reference_buttons.addStretch(1)
        reference_buttons.addWidget(remove_selected)
        reference_buttons.addWidget(clear_all)
        reference_layout.addLayout(reference_buttons)
        self.reference_strip = ReorderableImageStrip(icon_size=74)
        reference_layout.addWidget(self.reference_strip)
        self.reference_count_label = QLabel(
            "已选择 0 / 14 张 · 可直接拖入图片 · 拖动缩略图可调整顺序"
        )
        self.reference_count_label.setStyleSheet("color:#6d7a91;")
        self.reference_count_label.setWordWrap(True)
        reference_layout.addWidget(self.reference_count_label)
        form.addRow(self.reference_group)

        params_row = QWidget()
        params_layout = QHBoxLayout(params_row)
        params_layout.setContentsMargins(0, 0, 0, 0)
        self.aspect_combo = QComboBox()
        for value in ASPECT_RATIOS:
            self.aspect_combo.addItem(value, value)
        self.aspect_combo.setCurrentText(DEFAULT_ASPECT_RATIO)
        self.resolution_combo = QComboBox()
        for value in RESOLUTIONS:
            self.resolution_combo.addItem("自动（不发送）" if value == "auto" else value, value)
        self.resolution_combo.setCurrentIndex(
            next(i for i, value in enumerate(RESOLUTIONS) if value == DEFAULT_RESOLUTION)
        )
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 4)
        self.count_spin.setValue(DEFAULT_BATCH_SIZE)
        params_layout.addWidget(QLabel("宽高比"))
        params_layout.addWidget(self.aspect_combo)
        params_layout.addSpacing(8)
        params_layout.addWidget(QLabel("分辨率"))
        params_layout.addWidget(self.resolution_combo)
        params_layout.addSpacing(8)
        params_layout.addWidget(QLabel("数量"))
        params_layout.addWidget(self.count_spin)
        params_layout.addStretch(1)
        form.addRow("参数", params_row)

        self.generate_button = QPushButton("生成并加入队列")
        self.generate_button.setObjectName("PrimaryButton")
        self.generate_button.setMinimumHeight(42)
        form.addRow(self.generate_button)
        layout.addWidget(settings_card)
        self.queue_group = self._build_queue()
        layout.addWidget(self.queue_group)
        layout.addStretch(1)

        add_local.clicked.connect(self._add_local_references)
        add_folder.clicked.connect(self._add_local_reference_folder)
        add_library.clicked.connect(self._add_library_references)
        remove_selected.clicked.connect(self._remove_selected_reference)
        clear_all.clicked.connect(self._clear_references)
        self.crop_reference_button.clicked.connect(self._crop_selected_reference)
        self.restore_reference_button.clicked.connect(self._restore_selected_reference)
        self.template_combo.currentIndexChanged.connect(self._apply_template)
        save_template.clicked.connect(self._quick_save_template)
        manage_template.clicked.connect(self._manage_templates)
        self.generate_button.clicked.connect(self._enqueue_generation)
        self.reference_group.files_dropped.connect(self._on_reference_files_dropped)
        self.reference_strip.files_dropped.connect(self._on_reference_files_dropped)
        self.reference_strip.order_changed.connect(self._on_reference_order_changed)
        return scroll

    def _build_queue(self) -> QWidget:
        group = QGroupBox("任务队列")
        group.setObjectName("Card")
        group.setMinimumHeight(210)
        group.setMaximumHeight(280)
        layout = QVBoxLayout(group)
        top = QHBoxLayout()
        self.queue_hint = QLabel("串行执行；失败不会自动重试，避免重复扣费。")
        self.queue_hint.setStyleSheet("color:#6d7a91;")
        refresh = QPushButton("刷新")
        self.cancel_button = QPushButton("取消任务")
        top.addWidget(self.queue_hint, 1)
        top.addWidget(refresh)
        top.addWidget(self.cancel_button)
        layout.addLayout(top)
        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setHorizontalHeaderLabels(
            ["ID", "状态", "总耗时", "模式 / 模型", "提示词"]
        )
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setColumnWidth(0, 45)
        self.queue_table.setColumnWidth(1, 76)
        self.queue_table.setColumnWidth(2, 84)
        self.queue_table.setColumnWidth(3, 145)
        self.queue_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.queue_table, 1)
        refresh.clicked.connect(self.refresh_queue)
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.queue_table.cellDoubleClicked.connect(self._open_queue_row)
        self.queue_table.itemSelectionChanged.connect(self._queue_selection_changed)
        return group

    def _build_preview(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        header = QHBoxLayout()
        title = QLabel("参考图与结果")
        title.setStyleSheet("font-size:19px;font-weight:700;")
        self.task_status_label = QLabel("尚未选择任务")
        self.task_status_label.setStyleSheet("color:#68768d;")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.task_status_label)
        layout.addLayout(header)

        self.asset_strip = ImageStrip(icon_size=82)
        layout.addWidget(self.asset_strip)
        self.preview_stack = QStackedWidget()
        self.preview = ImageView()
        self.compare_widget = CompareWidget()
        self.preview_stack.addWidget(self.preview)
        self.preview_stack.addWidget(self.compare_widget)
        layout.addWidget(self.preview_stack, 1)

        self.task_detail_label = QLabel("")
        self.task_detail_label.setWordWrap(True)
        self.task_detail_label.setStyleSheet("color:#5d6b82;")
        layout.addWidget(self.task_detail_label)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setStyleSheet(
            "color:#2E64C4;font-weight:600;padding-top:2px;"
        )
        layout.addWidget(self.elapsed_label)

        actions = QHBoxLayout()
        self.compare_button = QPushButton("与参考图对比")
        self.save_local_button = QPushButton("保存到本地")
        self.save_library_button = QPushButton("保存到作品库")
        self.save_library_button.setObjectName("PrimaryButton")
        actions.addWidget(self.compare_button)
        actions.addStretch(1)
        actions.addWidget(self.save_local_button)
        actions.addWidget(self.save_library_button)
        layout.addLayout(actions)

        self.asset_strip.currentRowChanged.connect(self._show_selected_asset)
        self.compare_button.clicked.connect(self._compare_reference)
        self.save_local_button.clicked.connect(self._save_local_result)
        self.save_library_button.clicked.connect(self._save_library_result)
        self.save_local_button.setEnabled(False)
        self.save_library_button.setEnabled(False)
        self.compare_button.setEnabled(False)
        return card

    def _update_reference_visibility(self) -> None:
        is_reference = self.mode_combo.currentData() == MODE_REFERENCE
        self.reference_group.setVisible(is_reference)
        self.compare_button.setEnabled(is_reference and bool(self.current_results))

    def _update_reference_list(self) -> None:
        items = [
            (image.path, f"参考 {index + 1}", image)
            for index, image in enumerate(self.reference_images)
        ]
        if self.async_thumbnails:
            self.reference_strip.set_images_async(items)
        else:
            self.reference_strip.set_images(items)
        self.reference_count_label.setText(
            f"已选择 {len(self.reference_images)} / 14 张 · "
            "可直接拖入图片 · 拖动缩略图可调整顺序"
        )
        self.compare_button.setEnabled(
            self.mode_combo.currentData() == MODE_REFERENCE
            and bool(self.reference_images)
            and bool(self.current_results)
            and self._reference_list_matches_task()
        )
        self._update_reference_actions()

    def _add_stored_references(self, source_paths: list[Path]) -> None:
        remaining = 14 - len(self.reference_images)
        if remaining <= 0:
            QMessageBox.information(self, "已达到上限", "参考图最多选择 14 张。")
            return

        paths, scan_errors = scan_image_inputs(source_paths)
        known_hashes = {image.content_hash for image in self.reference_images}
        errors = list(scan_errors)
        added = 0
        duplicate_count = 0
        overflow = 0
        for index, path in enumerate(paths):
            if len(self.reference_images) >= 14:
                overflow = len(paths) - index
                break
            try:
                image = self.file_store.save_reference(path)
            except (OSError, ImageValidationError) as exc:
                errors.append(f"{path.name}：{exc}")
                continue
            if image.content_hash in known_hashes:
                duplicate_count += 1
                continue
            self.reference_images.append(image)
            known_hashes.add(image.content_hash)
            added += 1
        self._update_reference_list()
        summary = [f"已添加 {added} 张参考图"]
        if duplicate_count:
            summary.append(f"跳过 {duplicate_count} 张重复图片")
        if overflow:
            summary.append(f"因 14 张上限未导入 {overflow} 张")
        message = "，".join(summary) + "。"
        if errors:
            QMessageBox.warning(
                self,
                "部分图片未添加",
                message + "\n\n" + "\n".join(errors[:8]),
            )
        elif duplicate_count or overflow:
            QMessageBox.information(self, "添加结果", message)
        elif not added:
            QMessageBox.information(
                self,
                "没有可添加图片",
                "所选位置中没有支持的新图片。",
            )

    def _on_reference_files_dropped(self, paths: list[Path]) -> None:
        if not paths:
            return
        self._remember_reference_input_folder(paths)
        if self.mode_combo.currentData() != MODE_REFERENCE:
            self.mode_combo.setCurrentIndex(1)
        self._add_stored_references(paths)

    def _remember_reference_input_folder(self, paths: list[Path]) -> None:
        if not paths:
            return
        first = Path(paths[0])
        folder = first if first.is_dir() else first.parent
        self.app_settings.set_input_folder(INPUT_CREATE_REFERENCE, folder)

    def _on_reference_order_changed(self, images: list[StoredImage]) -> None:
        if len(images) != len(self.reference_images):
            return
        selected_row = self.reference_strip.currentRow()
        self.reference_images = list(images)
        self.reference_strip.blockSignals(True)
        for row, image in enumerate(self.reference_images):
            item = self.reference_strip.item(row)
            if item:
                item.setText(f"参考 {row + 1}")
                item.setData(Qt.ItemDataRole.UserRole, image)
        if 0 <= selected_row < self.reference_strip.count():
            self.reference_strip.setCurrentRow(selected_row)
        self.reference_strip.blockSignals(False)
        self._update_reference_actions()
        self._mark_reference_draft_changed()

    def _update_reference_actions(self) -> None:
        row = self.reference_strip.currentRow()
        has_reference = 0 <= row < len(self.reference_images)
        self.crop_reference_button.setEnabled(has_reference)
        image = self.reference_images[row] if has_reference else None
        self.restore_reference_button.setEnabled(
            bool(
                image
                and image.source_path
                and image.source_path.is_file()
            )
        )

    def _crop_selected_reference(self) -> None:
        row = self.reference_strip.currentRow()
        if not 0 <= row < len(self.reference_images):
            return
        dialog = CropDialog(self.reference_images[row].path, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        crop_box = dialog.crop_box()
        if crop_box is None:
            return
        try:
            cropped = self.file_store.save_cropped_reference(
                self.reference_images[row], crop_box
            )
        except Exception as exc:
            QMessageBox.warning(self, "裁剪失败", str(exc))
            return
        self.reference_images[row] = cropped
        self._update_reference_list()
        self.reference_strip.setCurrentRow(row)
        self._mark_reference_draft_changed()

    def _restore_selected_reference(self) -> None:
        row = self.reference_strip.currentRow()
        if not 0 <= row < len(self.reference_images):
            return
        image = self.reference_images[row]
        if not image.source_path or not image.source_path.is_file():
            QMessageBox.information(
                self, "无法恢复", "原始参考图文件已不存在。"
            )
            return
        try:
            restored = self.file_store.save_reference(image.source_path)
        except Exception as exc:
            QMessageBox.warning(self, "恢复失败", str(exc))
            return
        self.reference_images[row] = restored
        self._update_reference_list()
        self.reference_strip.setCurrentRow(row)
        self._mark_reference_draft_changed()

    def _mark_reference_draft_changed(self) -> None:
        if self.current_task_id is None:
            return
        if not self._reference_list_matches_task():
            self.task_status_label.setText("参考图草稿已修改，仅用于下一次生成")
            self.task_detail_label.setText(
                "已提交任务的参考图顺序和裁剪结果保持不变。"
            )

    def _preview_reference(self, row: int) -> None:
        if not 0 <= row < len(self.reference_images):
            return
        self.preview.set_image(self.reference_images[row].path)
        self.preview_stack.setCurrentIndex(0)
        self.save_local_button.setEnabled(False)
        self.save_library_button.setEnabled(False)
        self._update_reference_actions()
        if not self._reference_list_matches_task():
            self.compare_button.setEnabled(False)
            return
        for index, asset in enumerate(self.current_assets):
            if asset.is_reference and asset.position == row:
                if self.asset_strip.currentRow() != index:
                    self.asset_strip.setCurrentRow(index)
                break

    def _reference_list_matches_task(self) -> bool:
        task_references = [
            asset for asset in self.current_assets if asset.is_reference
        ]
        if len(task_references) != len(self.reference_images):
            return False
        return all(
            asset.payload.content_hash == reference.content_hash
            for asset, reference in zip(task_references, self.reference_images)
        )

    def _add_local_references(self) -> None:
        if self.mode_combo.currentData() != MODE_REFERENCE:
            self.mode_combo.setCurrentIndex(1)
        remaining = max(0, 14 - len(self.reference_images))
        if remaining <= 0:
            QMessageBox.information(self, "已达到上限", "参考图最多选择 14 张。")
            return
        initial = self.app_settings.input_folder(INPUT_CREATE_REFERENCE)
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择参考图",
            str(initial) if initial else "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;所有文件 (*)",
        )
        if paths:
            selected = [Path(path) for path in paths]
            self._remember_reference_input_folder(selected)
            self._add_stored_references(selected)

    def _add_local_reference_folder(self) -> None:
        if self.mode_combo.currentData() != MODE_REFERENCE:
            self.mode_combo.setCurrentIndex(1)
        remaining = 14 - len(self.reference_images)
        if remaining <= 0:
            QMessageBox.information(self, "已达到上限", "参考图最多选择 14 张。")
            return
        initial = self.app_settings.input_folder(INPUT_CREATE_REFERENCE)
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择参考图文件夹",
            str(initial) if initial else "",
        )
        if not directory:
            return
        folder = Path(directory)
        self.app_settings.set_input_folder(INPUT_CREATE_REFERENCE, folder)
        self._add_stored_references([folder])

    def _add_library_references(self) -> None:
        self.mode_combo.setCurrentIndex(1)
        remaining = 14 - len(self.reference_images)
        if remaining <= 0:
            QMessageBox.information(self, "已达到上限", "参考图最多选择 14 张。")
            return
        dialog = LibraryPickerDialog(self.database, remaining, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._add_stored_references(dialog.selected_paths())

    def _remove_selected_reference(self) -> None:
        row = self.reference_strip.currentRow()
        if 0 <= row < len(self.reference_images):
            self.reference_images.pop(row)
            self._update_reference_list()

    def _clear_references(self) -> None:
        self.reference_images.clear()
        self._update_reference_list()

    def refresh(self) -> None:
        self.refresh_templates()
        self.refresh_queue()

    def refresh_templates(self) -> None:
        selected = self.template_combo.currentData()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("选择提示词模板", None)
        for template in self.database.list_templates():
            self.template_combo.addItem(template["name"], template)
        if selected:
            for index in range(self.template_combo.count()):
                data = self.template_combo.itemData(index)
                if data and data.get("id") == selected.get("id"):
                    self.template_combo.setCurrentIndex(index)
                    break
        self.template_combo.blockSignals(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_templates()

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
        if dialog.selected_prompt:
            self.prompt_edit.setPlainText(dialog.selected_prompt)
        self.refresh_templates()
        if dialog.templates_changed:
            self.templates_changed.emit()

    def _enqueue_generation(self) -> None:
        if not self.secret_store.load():
            QMessageBox.warning(
                self,
                "API Key 不可用",
                "当前账号尚未配置 API Key，请联系管理员。",
            )
            return
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.information(self, "缺少提示词", "请先填写提示词。")
            return
        mode = self.mode_combo.currentData()
        references = tuple(self.reference_images) if mode == MODE_REFERENCE else ()
        if mode == MODE_REFERENCE and not references:
            QMessageBox.information(self, "缺少参考图", "参考图编辑至少需要一张图片。")
            return
        resolution = self.resolution_combo.currentData()
        selected_model = self.model_combo.currentData()
        auto_strategy = None
        auto_reason = None
        if selected_model == AUTO_MODEL_ID:
            decision = choose_model(
                prompt=prompt,
                mode=mode,
                reference_count=len(references),
                strategy=self.app_settings.auto_strategy(),
            )
            selected_model = decision.model
            auto_strategy = decision.strategy
            auto_reason = decision.reason
        request = ImageRequest(
            model=selected_model,
            prompt=prompt,
            n=self.count_spin.value(),
            aspect_ratio=self.aspect_combo.currentData(),
            resolution=resolution,
            reference_images=references,
            mode=mode,
            auto_strategy=auto_strategy,
            auto_reason=auto_reason,
        )
        try:
            request.validate()
            self.current_task_id = self.database.create_task(request)
        except ValueError as exc:
            QMessageBox.warning(self, "参数不正确", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "无法创建任务", str(exc))
            return
        self.refresh_queue()
        self.worker.notify()
        self.task_status_label.setText(f"任务 #{self.current_task_id} 已加入队列")
        self.task_detail_label.setText("参数已保存，任务将按顺序执行。")
        self.preview.set_placeholder("任务排队中，完成后结果会显示在这里")

    def refresh_queue(self) -> None:
        self._updating_queue = True
        self.queue_table.blockSignals(True)
        selected_task_id = None
        selected = self.queue_table.currentRow()
        if selected >= 0:
            item = self.queue_table.item(selected, 0)
            if item:
                selected_task_id = item.data(Qt.ItemDataRole.UserRole)
        try:
            tasks = self.database.list_tasks(limit=40)
            self.queue_table.setRowCount(len(tasks))
            for row, task in enumerate(tasks):
                id_item = QTableWidgetItem(str(task["id"]))
                id_item.setData(Qt.ItemDataRole.UserRole, task["id"])
                id_item.setData(TASK_DATA_ROLE, task)
                status_text = STATUS_LABELS.get(task["status"], task["status"])
                if task["status"] == TASK_RUNNING and task["cancel_requested"]:
                    status_text = "停止等待中"
                status_item = QTableWidgetItem(status_text)
                status_item.setData(Qt.ItemDataRole.UserRole, task["status"])
                model_name = MODEL_LABELS.get(task["model"], task["model"])
                if task["auto_strategy"]:
                    model_name = f"{model_name} · 自动"
                mode_item = QTableWidgetItem(
                    f"{MODE_LABELS.get(task['mode'], task['mode'])} · {model_name}"
                )
                elapsed_item = QTableWidgetItem(format_task_elapsed(task))
                prompt_item = QTableWidgetItem(task["prompt"].replace("\n", " ")[:80])
                prompt_item.setToolTip(task["prompt"])
                colors = {
                    TASK_SUCCESS: QColor("#1f7a4d"),
                    TASK_FAILED: QColor("#b72f3c"),
                    TASK_RUNNING: QColor("#245ab8"),
                    TASK_CANCELLED: QColor("#7d8797"),
                }
                if task["status"] in colors:
                    status_item.setForeground(colors[task["status"]])
                self.queue_table.setItem(row, 0, id_item)
                self.queue_table.setItem(row, 1, status_item)
                self.queue_table.setItem(row, 2, elapsed_item)
                self.queue_table.setItem(row, 3, mode_item)
                self.queue_table.setItem(row, 4, prompt_item)
                if selected_task_id == task["id"]:
                    self.queue_table.selectRow(row)
        finally:
            self.queue_table.blockSignals(False)
            self._updating_queue = False

    def _open_queue_row(self, row: int, column: int) -> None:
        item = self.queue_table.item(row, 0)
        if item:
            self.load_task_into_form(int(item.data(Qt.ItemDataRole.UserRole)))

    def _queue_selection_changed(self) -> None:
        if self._updating_queue:
            return
        row = self.queue_table.currentRow()
        if row < 0:
            return
        item = self.queue_table.item(row, 0)
        if item:
            self.load_task_into_form(int(item.data(Qt.ItemDataRole.UserRole)))

    def load_task_into_form(self, task_id: int) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            return
        self.current_task_id = task_id
        self.current_task_snapshot = task
        self.current_results = []
        self.current_assets = []
        model_index = self.model_combo.findData(task["model"])
        if model_index >= 0:
            self.model_combo.setCurrentIndex(model_index)
        mode_index = self.mode_combo.findData(task["mode"])
        if mode_index < 0 and task["mode"] in (MODE_DRESS_UP, MODE_FREE_COMPOSE):
            mode_index = self.mode_combo.findData(MODE_REFERENCE)
        if mode_index >= 0:
            self.mode_combo.setCurrentIndex(mode_index)
        self.prompt_edit.setPlainText(task["prompt"])
        aspect_index = self.aspect_combo.findData(task["aspect_ratio"])
        if aspect_index >= 0:
            self.aspect_combo.setCurrentIndex(aspect_index)
        resolution = task["resolution"] or "auto"
        resolution_index = self.resolution_combo.findData(resolution)
        if resolution_index >= 0:
            self.resolution_combo.setCurrentIndex(resolution_index)
        self.count_spin.setValue(int(task["n"]))
        self.reference_images = list(self.database.get_task_references(task_id))
        self._update_reference_list()
        self._update_reference_visibility()
        self.show_task(task_id)

    def prepare_retry(self, task_id: int) -> None:
        self.load_task_into_form(task_id)
        self.task_status_label.setText(
            f"已载入任务 #{task_id}，修改参数后重新生成"
        )
        self.task_detail_label.setText(
            "当前显示的是原任务结果。修改模型、提示词或参数后，"
            "点击“生成并加入队列”才会创建新任务。"
        )
        self.prompt_edit.setFocus()
        cursor = self.prompt_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.prompt_edit.setTextCursor(cursor)

    def _cancel_selected(self) -> None:
        row = self.queue_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择任务", "请先在队列表中选择一个任务。")
            return
        item = self.queue_table.item(row, 0)
        status_item = self.queue_table.item(row, 1)
        task_id = int(item.data(Qt.ItemDataRole.UserRole))
        status = status_item.data(Qt.ItemDataRole.UserRole)
        if status == TASK_RUNNING:
            answer = QMessageBox.warning(
                self,
                "停止等待",
                "当前请求无法保证在服务端停止，仍可能产生费用。\n确定停止等待吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.worker.request_cancel(task_id)
            self.task_status_label.setText(f"任务 #{task_id} 已停止等待服务端结果")
        elif status == TASK_QUEUED:
            self.worker.request_cancel(task_id)
        else:
            QMessageBox.information(self, "无需取消", "该任务已经结束。")
            return
        self.refresh_queue()

    def _on_task_changed(self, task_id: int, status: str) -> None:
        self.refresh_queue()
        if self.current_task_id in (None, task_id) or status == TASK_SUCCESS:
            self.current_task_id = task_id
            self.show_task(task_id)

    def show_task(self, task_id: int) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            return
        self.current_task_id = task_id
        self.current_task_snapshot = task
        status_text = STATUS_LABELS.get(task["status"], task["status"])
        if task["status"] == TASK_RUNNING and task["cancel_requested"]:
            status_text = "停止等待中"
        model_name = MODEL_LABELS.get(task["model"], task["model"])
        self.task_status_label.setText(f"任务 #{task_id} · {status_text}")
        details = [
            f"{MODE_LABELS.get(task['mode'], task['mode'])} · {model_name}",
            f"宽高比 {task['aspect_ratio']} · 分辨率 {task['resolution'] or '自动'} · 数量 {task['n']}",
        ]
        if task["auto_strategy"]:
            details.append(
                "自动策略："
                + AUTO_STRATEGY_LABELS.get(task["auto_strategy"], task["auto_strategy"])
            )
        if task["auto_reason"]:
            details.append(f"判断：{task['auto_reason']}")
        if task["request_id"]:
            details.append(f"Request ID：{task['request_id']}")
        if task["remote_task_id"]:
            details.append(f"任务 ID：{task['remote_task_id']}")
        if task["input_tokens"] is not None or task["output_tokens"] is not None:
            details.append(
                f"用量：输入 {task['input_tokens'] or 0} / 输出 {task['output_tokens'] or 0}"
            )
        if task["error_message"]:
            details.append(f"错误：{task['error_message']}")
        self.task_detail_label.setText("\n".join(details))
        self.elapsed_label.setText(f"总耗时：{format_task_elapsed(task)}")
        self.current_results = self.database.get_task_results(task_id)
        references = self.database.get_task_references(task_id)
        self.current_assets = build_task_assets(references, self.current_results)
        items = [
            (asset.thumbnail_path, asset.label, asset)
            for asset in self.current_assets
        ]
        if self.async_thumbnails:
            self.asset_strip.set_images_async(items)
        else:
            self.asset_strip.set_images(items)
        self.compare_button.setEnabled(
            any(asset.is_reference for asset in self.current_assets)
            and any(asset.is_result for asset in self.current_assets)
            and self._reference_list_matches_task()
        )
        if self.current_assets:
            self.preview_stack.setCurrentIndex(0)
            default_row = next(
                (
                    index
                    for index, asset in enumerate(self.current_assets)
                    if asset.is_result
                ),
                0,
            )
            self.asset_strip.blockSignals(True)
            self.asset_strip.setCurrentRow(default_row)
            self.asset_strip.blockSignals(False)
            self._show_selected_asset(default_row)
        else:
            self.save_local_button.setEnabled(False)
            self.save_library_button.setEnabled(False)
            if task["status"] == TASK_FAILED:
                self.preview.set_placeholder(task["error_message"] or "任务失败")
            elif task["status"] == TASK_CANCELLED:
                self.preview.set_placeholder("任务已取消")
            elif task["status"] == TASK_RUNNING:
                self.preview.set_placeholder("正在生成，请稍候……")
            else:
                self.preview.set_placeholder("任务排队中，完成后结果会显示在这里")
            self.preview.set_image(None)
            self.preview_stack.setCurrentIndex(0)

    def _update_elapsed_displays(self) -> None:
        for row in range(self.queue_table.rowCount()):
            item = self.queue_table.item(row, 0)
            task = item.data(TASK_DATA_ROLE) if item else None
            if task:
                elapsed_item = self.queue_table.item(row, 2)
                if elapsed_item is None:
                    self.queue_table.setItem(
                        row, 2, QTableWidgetItem(format_task_elapsed(task))
                    )
                else:
                    elapsed_item.setText(format_task_elapsed(task))
        if self.current_task_snapshot:
            self.elapsed_label.setText(
                f"总耗时：{format_task_elapsed(self.current_task_snapshot)}"
            )
        else:
            self.elapsed_label.clear()

    def _selected_asset(self) -> TaskImageAsset | None:
        row = self.asset_strip.currentRow()
        if 0 <= row < len(self.current_assets):
            return self.current_assets[row]
        return None

    def _show_selected_asset(self, row: int) -> None:
        if not 0 <= row < len(self.current_assets):
            self.preview.set_image(None)
            return
        asset = self.current_assets[row]
        self.preview.set_image(asset.path)
        self.preview_stack.setCurrentIndex(0)
        if asset.is_result:
            saved = bool(asset.payload["saved"])
            self.save_local_button.setEnabled(True)
            self.save_library_button.setText(
                "已保存到作品库" if saved else "保存到作品库"
            )
            self.save_library_button.setEnabled(not saved)
        else:
            self.save_local_button.setEnabled(False)
            self.save_library_button.setText("保存到作品库")
            self.save_library_button.setEnabled(False)
        self.compare_button.setEnabled(
            any(asset.is_reference for asset in self.current_assets)
            and any(asset.is_result for asset in self.current_assets)
            and self._reference_list_matches_task()
        )

    def _save_local_result(self) -> None:
        if self.current_task_id is None:
            return
        asset = self._selected_asset()
        if asset is None or not asset.is_result:
            return
        result = asset.payload
        saved_path = self.local_save_controller.save(
            self.current_task_id, int(result["id"])
        )
        if saved_path:
            show_saved_toast(self, saved_path)

    def _save_library_result(self) -> None:
        if self.current_task_id is None:
            return
        asset = self._selected_asset()
        if asset is None or not asset.is_result:
            return
        result = asset.payload
        try:
            work_id = self.library_service.save_task_result(
                self.current_task_id, int(result["id"])
            )
            folder = self.library_service.work_folder(work_id)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.library_changed.emit()
        self.show_task(self.current_task_id)
        show_saved_path(
            self,
            title="已保存到作品库",
            message="图片和参数已保存到作品库。",
            path=folder,
        )

    def _compare_reference(self) -> None:
        if self.current_task_id is None:
            return
        selected = self._selected_asset()
        references = [
            asset for asset in self.current_assets if asset.is_reference
        ]
        results = [asset for asset in self.current_assets if asset.is_result]
        if not references or not results:
            return
        if selected and selected.is_reference:
            left_asset = selected
            right_asset = results[0]
        else:
            left_asset = references[0]
            right_asset = selected if selected and selected.is_result else results[0]
        dialog = CompareDialog(
            left_asset.path,
            right_asset.path,
            left_label=left_asset.label,
            right_label=right_asset.label,
            parent=self,
        )
        dialog.exec()
