from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.folders import (
    AccountDirectory,
    default_account_directory,
    natural_sort_key,
    project_assets,
    resolve_account_directory,
    scan_product_folders,
)
from ..collaboration.matching import MatchAsset, build_pairs
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    PAIRING_ALL,
    PAIRING_MANUAL,
    ROLE_ADMIN,
    ROLE_OPERATIONS,
    AuthUser,
    PairingInput,
    ProjectDraft,
)
from ..storage import SUPPORTED_IMAGE_SUFFIXES, atomic_copy_file, image_metadata
from .widgets import load_scaled_pixmap


@dataclass
class ProductDraftState:
    product_code: str
    path: Path
    garments: list[Path]
    models: list[Path]
    errors: list[str] = field(default_factory=list)
    manual_pairs: list[PairingInput] = field(default_factory=list)
    existing_tasks: list = field(default_factory=list)


class PairPreviewCard(QFrame):
    def __init__(
        self,
        model_path: Path,
        garment_paths: list[Path],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PairPreviewCard")
        self.setStyleSheet(
            "QFrame#PairPreviewCard {"
            "background:#f8faff;border:1px solid #dce5f2;"
            "border-radius:8px;padding:8px;}"
        )
        layout = QHBoxLayout(self)
        model = QLabel()
        model.setFixedSize(92, 112)
        model.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = load_scaled_pixmap(model_path, QSize(88, 108))
        if not pixmap.isNull():
            model.setPixmap(pixmap)
        layout.addWidget(model)
        info = QVBoxLayout()
        title = QLabel(model_path.name)
        title.setStyleSheet("font-weight:700;color:#2d3e58;")
        summary = QLabel(
            f"衣服 {len(garment_paths)} 张 · 1 个子任务"
        )
        summary.setStyleSheet("color:#60708a;")
        thumb_host = QWidget()
        thumbs = QHBoxLayout(thumb_host)
        thumbs.setContentsMargins(0, 0, 0, 0)
        thumbs.setSpacing(6)
        self.garment_thumbnails: list[QLabel] = []
        for path in garment_paths:
            image = QLabel()
            image.setObjectName("GarmentThumbnail")
            image.setFixedSize(58, 70)
            image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb = load_scaled_pixmap(path, QSize(56, 68))
            if not thumb.isNull():
                image.setPixmap(thumb)
            image.setToolTip(path.name)
            thumbs.addWidget(image)
            self.garment_thumbnails.append(image)
        thumb_scroll = QScrollArea()
        thumb_scroll.setWidgetResizable(False)
        thumb_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        thumb_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        thumb_scroll.setWidget(thumb_host)
        thumb_host.adjustSize()
        info.addWidget(title)
        info.addWidget(summary)
        info.addWidget(thumb_scroll)
        layout.addLayout(info, 1)


class TaskCreatePage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.states: dict[str, ProductDraftState] = {}
        self.current_state: ProductDraftState | None = None
        self.account_directory: AccountDirectory | None = None
        self._build()
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("新建任务")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel("自动扫描共享盘货号目录，确认后批量创建任务。")
        subtitle.setStyleSheet("color:#6d7a91;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.refresh_button = QPushButton("刷新扫描")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        outer.addLayout(header)

        path_card = QGroupBox("账号目录")
        path_layout = QVBoxLayout(path_card)
        self.account_path = QLineEdit()
        self.account_path.returnPressed.connect(self.refresh)
        choose_directory = QPushButton("选择目录")
        choose_directory.clicked.connect(self._choose_directory)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("账号目录"))
        path_row.addWidget(self.account_path, 1)
        path_row.addWidget(choose_directory)
        path_layout.addLayout(path_row)
        self.date_hint = QLabel()
        self.date_hint.setStyleSheet("color:#60708a;")
        path_layout.addWidget(self.date_hint)
        outer.addWidget(path_card)

        scan_card = QGroupBox("待创建货号")
        scan_layout = QVBoxLayout(scan_card)
        scan_actions = QHBoxLayout()
        self.select_new_button = QPushButton("全选未处理")
        self.select_new_button.clicked.connect(self._select_new)
        scan_actions.addWidget(self.select_new_button)
        scan_actions.addStretch(1)
        self.scan_summary = QLabel()
        scan_actions.addWidget(self.scan_summary)
        scan_layout.addLayout(scan_actions)
        self.product_table = QTableWidget(0, 8)
        self.product_table.setHorizontalHeaderLabels(
            [
                "创建",
                "货号",
                "衣服图",
                "模特图",
                "子任务",
                "预计任务编码",
                "重复情况",
                "状态",
            ]
        )
        self.product_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.product_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.product_table.horizontalHeader().setStretchLastSection(True)
        self.product_table.itemSelectionChanged.connect(self._product_selected)
        scan_layout.addWidget(self.product_table, 1)
        outer.addWidget(scan_card, 1)

        detail_card = QGroupBox("素材与配对预览")
        detail_layout = QVBoxLayout(detail_card)
        mode_row = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("自动配对（每张模特配全部衣服）", PAIRING_ALL)
        self.mode_combo.addItem("手动配对", PAIRING_MANUAL)
        self.mode_combo.currentIndexChanged.connect(self._pairing_changed)
        mode_row.addWidget(QLabel("配对方式"))
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        self.detail_title = QLabel("选择一个货号查看图片")
        mode_row.addWidget(self.detail_title)
        detail_layout.addLayout(mode_row)

        assets_row = QHBoxLayout()
        self.garment_list, garment_box = self._asset_box("衣服图", "garment")
        self.model_list, model_box = self._asset_box("模特图", "model")
        assets_row.addWidget(garment_box, 1)
        assets_row.addWidget(model_box, 1)
        detail_layout.addLayout(assets_row)

        self.manual_box = QGroupBox("手动配对")
        manual_layout = QVBoxLayout(self.manual_box)
        manual_actions = QHBoxLayout()
        garment_hint = QLabel("在左侧衣服图列表按住 Ctrl 可多选")
        add_pair = QPushButton("添加配对")
        remove_pair = QPushButton("移除所选配对")
        add_pair.clicked.connect(self._add_manual_pair)
        remove_pair.clicked.connect(self._remove_manual_pair)
        manual_actions.addWidget(garment_hint, 1)
        manual_actions.addWidget(add_pair)
        manual_actions.addWidget(remove_pair)
        manual_layout.addLayout(manual_actions)
        self.manual_table = QTableWidget(0, 2)
        self.manual_table.setHorizontalHeaderLabels(["模特图", "衣服图"])
        self.manual_table.horizontalHeader().setStretchLastSection(True)
        self.manual_table.setMaximumHeight(110)
        manual_layout.addWidget(self.manual_table)
        detail_layout.addWidget(self.manual_box)

        self.pair_preview = QScrollArea()
        self.pair_preview.setWidgetResizable(True)
        self.pair_preview.setMinimumHeight(180)
        self.pair_preview_content = QWidget()
        self.pair_preview_layout = QVBoxLayout(self.pair_preview_content)
        self.pair_preview_layout.setContentsMargins(0, 0, 0, 0)
        self.pair_preview_layout.setSpacing(6)
        self.pair_preview_layout.addStretch(1)
        self.pair_preview.setWidget(self.pair_preview_content)
        detail_layout.addWidget(self.pair_preview, 1)
        self.pair_summary = QLabel()
        self.pair_summary.setWordWrap(True)
        detail_layout.addWidget(self.pair_summary)
        outer.addWidget(detail_card, 1)

        footer = QHBoxLayout()
        footer.addWidget(QLabel("备注"))
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText(
            "选填，会应用到本次创建的任务"
        )
        footer.addWidget(self.note_edit, 1)
        footer.addStretch(1)
        self.create_button = QPushButton("创建所选任务")
        self.create_button.setObjectName("PrimaryButton")
        self.create_button.setMinimumHeight(42)
        self.create_button.clicked.connect(self._create_selected)
        self.create_button.setEnabled(
            self.actor.role in (ROLE_OPERATIONS, ROLE_ADMIN)
        )
        footer.addWidget(self.create_button)
        outer.addLayout(footer)
        self._update_mode_visibility()

    def _asset_box(self, title: str, role: str) -> tuple[QListWidget, QGroupBox]:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        buttons = QHBoxLayout()
        add_folder = QPushButton("选择文件夹")
        add_images = QPushButton("添加图片")
        remove = QPushButton("移除所选")
        clear = QPushButton("清空")
        add_folder.clicked.connect(lambda: self._add_folder(role))
        add_images.clicked.connect(lambda: self._add_images(role))
        remove.clicked.connect(lambda: self._remove_assets(role))
        clear.clicked.connect(lambda: self._clear_assets(role))
        for button in (add_folder, add_images, remove, clear):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        images = QListWidget()
        images.setViewMode(QListWidget.ViewMode.IconMode)
        images.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        images.setIconSize(QSize(96, 96))
        images.setGridSize(QSize(120, 125))
        images.setResizeMode(QListWidget.ResizeMode.Adjust)
        images.setMinimumHeight(145)
        layout.addWidget(images)
        return images, box

    def refresh(self) -> None:
        shared_root = self.repository.get_shared_setting("share_root")
        if shared_root:
            self.resolver = SharedPathResolver(shared_root)
        raw_path = self.account_path.text().strip()
        if not raw_path:
            raw_path = str(
                default_account_directory(
                    self.resolver.root,
                    self.actor.display_name,
                )
            )
        try:
            account_directory = resolve_account_directory(
                raw_path,
                self.resolver.root,
            )
            account_directory.path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            QMessageBox.warning(self, "无法访问共享盘", str(exc))
            return
        self.account_path.setText(str(account_directory.path))
        self.account_directory = account_directory
        if account_directory.date_recognized:
            self.date_hint.setText(
                f"已识别任务日期：{account_directory.source_date.isoformat()}"
            )
            self.date_hint.setStyleSheet("color:#2a7a50;")
        else:
            self.date_hint.setText(
                "目录末尾不是日期格式，将按今天生成任务编码。"
            )
            self.date_hint.setStyleSheet("color:#b25a24;")
        self._scan_directory()

    def _choose_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择账号日期目录",
            self.account_path.text(),
        )
        if selected:
            self.account_path.setText(selected)
            self.refresh()

    def _scan_directory(self) -> None:
        if self.account_directory is None:
            return
        try:
            products = scan_product_folders(self.account_directory.path)
        except Exception as exc:
            QMessageBox.warning(self, "扫描失败", str(exc))
            return
        self.states = {}
        for product in products:
            existing = self.repository.find_tasks_by_product(
                self.actor.id,
                self.account_directory.source_date,
                product.product_code,
            )
            self.states[product.product_code] = ProductDraftState(
                product_code=product.product_code,
                path=product.path,
                garments=list(product.garments),
                models=list(product.models),
                errors=list(product.errors),
                existing_tasks=existing,
            )
        self._refresh_product_table()
        if self.product_table.rowCount():
            self.product_table.selectRow(0)
        else:
            self.current_state = None
            self.detail_title.setText("当前日期没有符合结构的货号目录")
            self.garment_list.clear()
            self.model_list.clear()
            self._clear_pair_preview()

    def _refresh_product_table(self) -> None:
        self.product_table.setRowCount(0)
        selectable = 0
        for state in sorted(
            self.states.values(),
            key=lambda item: item.product_code.casefold(),
        ):
            row = self.product_table.rowCount()
            self.product_table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            can_select = not state.errors and not state.existing_tasks
            check.setCheckState(
                Qt.CheckState.Checked if can_select else Qt.CheckState.Unchecked
            )
            if can_select:
                selectable += 1
            self.product_table.setItem(row, 0, check)
            values = (
                state.product_code,
                str(len(state.garments)),
                str(len(state.models)),
                str(len(state.models)),
                f"{self.actor.display_name}-{state.product_code}-自动流水",
                (
                    f"已有 {len(state.existing_tasks)} 个任务"
                    if state.existing_tasks
                    else "无重复"
                ),
                "可创建" if can_select else "需处理",
            )
            for column, value in enumerate(values, start=1):
                self.product_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )
            self.product_table.item(row, 0).setData(
                Qt.ItemDataRole.UserRole,
                state.product_code,
            )
        self.scan_summary.setText(
            f"共 {len(self.states)} 个货号，可直接创建 {selectable} 个"
        )

    def _selected_product_code(self) -> str | None:
        row = self.product_table.currentRow()
        if row < 0:
            return None
        item = self.product_table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _product_selected(self) -> None:
        code = self._selected_product_code()
        self.current_state = self.states.get(code) if code else None
        self._show_state()

    def _show_state(self) -> None:
        state = self.current_state
        self.garment_list.clear()
        self.model_list.clear()
        self.manual_table.setRowCount(0)
        self._clear_pair_preview()
        if state is None:
            return
        self.detail_title.setText(
            f"{state.product_code} · 衣服 {len(state.garments)} 张 · "
            f"模特 {len(state.models)} 张"
        )
        self._fill_assets(self.garment_list, state.garments)
        self._fill_assets(self.model_list, state.models)
        self._refresh_manual_table()
        self._refresh_pair_preview()

    @staticmethod
    def _fill_assets(widget: QListWidget, paths: list[Path]) -> None:
        for path in paths:
            item = QListWidgetItem(path.name)
            pixmap = load_scaled_pixmap(path, QSize(96, 96))
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            widget.addItem(item)

    def _clear_pair_preview(self) -> None:
        while self.pair_preview_layout.count():
            item = self.pair_preview_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _selected_paths(self, role: str) -> list[Path]:
        widget = self.garment_list if role == "garment" else self.model_list
        paths = []
        for item in widget.selectedItems():
            raw = item.data(Qt.ItemDataRole.UserRole)
            if raw:
                paths.append(Path(raw))
        return paths

    def _asset_paths(self, role: str) -> list[Path]:
        state = self.current_state
        if state is None:
            return []
        return state.garments if role == "garment" else state.models

    def _import_paths(self, role: str, paths: list[Path]) -> None:
        state = self.current_state
        if state is None:
            return
        role_folder = state.path / ("衣服图" if role == "garment" else "模特图")
        role_folder.mkdir(parents=True, exist_ok=True)
        target = state.garments if role == "garment" else state.models
        known = {path.resolve() for path in target}
        added = 0
        errors: list[str] = []
        for source in paths:
            try:
                if source.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                    continue
                if source.resolve().parent == role_folder.resolve():
                    destination = source.resolve()
                else:
                    destination = _unique_destination(role_folder, source)
                    atomic_copy_file(source.resolve(), destination)
                data = destination.read_bytes()
                image_metadata(data)
                if destination.resolve() in known:
                    continue
                target.append(destination.resolve())
                known.add(destination.resolve())
                added += 1
            except Exception as exc:
                errors.append(f"{source.name}：{exc}")
        if role == "garment":
            state.garments.sort(key=lambda path: natural_sort_key(path.name))
        else:
            state.models.sort(key=lambda path: natural_sort_key(path.name))
        state.manual_pairs.clear()
        if errors:
            QMessageBox.warning(
                self,
                "部分图片未导入",
                "\n".join(errors[:8]),
            )
        if not added and not errors:
            QMessageBox.information(self, "导入结果", "没有新增图片。")
        self._show_state()

    def _add_folder(self, role: str) -> None:
        state = self.current_state
        if state is None:
            QMessageBox.information(self, "请选择货号", "请先在列表中选择一个货号。")
            return
        folder = QFileDialog.getExistingDirectory(self, "选择图片文件夹")
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
        self._import_paths(role, paths)

    def _add_images(self, role: str) -> None:
        state = self.current_state
        if state is None:
            QMessageBox.information(self, "请选择货号", "请先在列表中选择一个货号。")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        self._import_paths(role, [Path(path) for path in files])

    def _remove_assets(self, role: str) -> None:
        state = self.current_state
        if state is None:
            return
        selected = set(self._selected_paths(role))
        target = state.garments if role == "garment" else state.models
        target[:] = [path for path in target if path not in selected]
        state.manual_pairs.clear()
        self._show_state()

    def _clear_assets(self, role: str) -> None:
        state = self.current_state
        if state is None:
            return
        target = state.garments if role == "garment" else state.models
        target.clear()
        state.manual_pairs.clear()
        self._show_state()

    def _pairing_changed(self) -> None:
        self._update_mode_visibility()
        self._refresh_pair_preview()

    def _update_mode_visibility(self) -> None:
        self.manual_box.setVisible(
            self.mode_combo.currentData() == PAIRING_MANUAL
        )

    def _current_pairs(self, state: ProductDraftState | None = None):
        current = state or self.current_state
        if current is None:
            return None
        garments = [
            MatchAsset("", path.name)
            for path in current.garments
        ]
        models = [
            MatchAsset("", path.name)
            for path in current.models
        ]
        return build_pairs(
            garments,
            models,
            self.mode_combo.currentData(),
            current.manual_pairs,
        )

    def _refresh_pair_preview(self) -> None:
        state = self.current_state
        if state is None:
            return
        result = self._current_pairs()
        while self.pair_preview_layout.count():
            item = self.pair_preview_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if result is None:
            return
        if result.errors:
            self.pair_summary.setText("暂不能创建：" + "；".join(result.errors[:5]))
            self.pair_summary.setStyleSheet("color:#b23a48;")
            return
        grouped: dict[int, list[int]] = {}
        for pair in result.pairs:
            grouped.setdefault(pair.model_index, []).append(
                pair.garment_index
            )
        for model_index in sorted(grouped):
            model_path = state.models[model_index]
            garment_paths = [
                state.garments[index]
                for index in dict.fromkeys(grouped[model_index])
            ]
            self.pair_preview_layout.addWidget(
                PairPreviewCard(model_path, garment_paths)
            )
        self.pair_preview_layout.addStretch(1)
        self.pair_summary.setText(
            f"将创建 {len(grouped)} 个子任务，共引用 {len(result.pairs)} 组衣服。"
        )
        self.pair_summary.setStyleSheet("color:#2a7a50;")

    def _add_manual_pair(self) -> None:
        state = self.current_state
        if state is None:
            return
        model_items = self.model_list.selectedItems()
        garment_items = self.garment_list.selectedItems()
        if len(model_items) != 1 or not garment_items:
            QMessageBox.information(
                self,
                "选择不完整",
                "请选择 1 张模特图和至少 1 张衣服图。",
            )
            return
        model_path = Path(model_items[0].data(Qt.ItemDataRole.UserRole))
        model_index = state.models.index(model_path)
        added = 0
        for garment_item in garment_items:
            garment_path = Path(
                garment_item.data(Qt.ItemDataRole.UserRole)
            )
            pair = PairingInput(
                state.garments.index(garment_path),
                model_index,
            )
            if pair not in state.manual_pairs:
                state.manual_pairs.append(pair)
                added += 1
        self._refresh_manual_table()
        self._refresh_pair_preview()

    def _remove_manual_pair(self) -> None:
        state = self.current_state
        if state is None:
            return
        row = self.manual_table.currentRow()
        if 0 <= row < len(state.manual_pairs):
            state.manual_pairs.pop(row)
            self._refresh_manual_table()
            self._refresh_pair_preview()

    def _refresh_manual_table(self) -> None:
        state = self.current_state
        self.manual_table.setRowCount(0)
        if state is None:
            return
        for pair in state.manual_pairs:
            row = self.manual_table.rowCount()
            self.manual_table.insertRow(row)
            self.manual_table.setItem(
                row,
                0,
                QTableWidgetItem(state.models[pair.model_index].name),
            )
            self.manual_table.setItem(
                row,
                1,
                QTableWidgetItem(state.garments[pair.garment_index].name),
            )

    def _select_new(self) -> None:
        for row in range(self.product_table.rowCount()):
            code = self.product_table.item(row, 0).data(
                Qt.ItemDataRole.UserRole
            )
            state = self.states.get(code)
            if state and not state.existing_tasks and not state.errors:
                self.product_table.item(row, 0).setCheckState(
                    Qt.CheckState.Checked
                )

    def _selected_codes(self) -> list[str]:
        codes: list[str] = []
        for row in range(self.product_table.rowCount()):
            check = self.product_table.item(row, 0)
            if check.checkState() == Qt.CheckState.Checked:
                codes.append(str(check.data(Qt.ItemDataRole.UserRole)))
        return codes

    def _create_selected(self) -> None:
        codes = self._selected_codes()
        if not codes:
            QMessageBox.warning(self, "未选择货号", "请至少勾选一个货号。")
            return
        account_directory = self.account_directory
        if account_directory is None:
            return
        answer = QMessageBox.question(
            self,
            "确认创建",
            f"确认创建 {len(codes)} 个总任务吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        created = 0
        try:
            for code in codes:
                state = self.states[code]
                if state.errors:
                    raise ValueError(f"{code}：{'；'.join(state.errors)}")
                if not state.garments or not state.models:
                    raise ValueError(f"{code}：衣服图和模特图不能为空")
                result = self._current_pairs(state)
                if result is None or result.errors:
                    self.product_table.selectRow(
                        self._row_for_code(code)
                    )
                    raise ValueError(
                        f"{code}："
                        + "；".join(result.errors if result else ["配对失败"])
                    )
                assets = project_assets(
                    state.path,
                    state.garments,
                    state.models,
                )
                draft = ProjectDraft(
                    project_code="",
                    name=state.product_code,
                    deadline=datetime.now(),
                    priority="normal",
                    project_relative_root=self.resolver.relative_to_root(
                        state.path
                    ),
                    pairing_mode=self.mode_combo.currentData(),
                    assets=assets,
                    pairs=result.pairs,
                    note=self.note_edit.text().strip(),
                    source_date=account_directory.source_date,
                    product_code=state.product_code,
                )
                existing = state.existing_tasks[0] if state.existing_tasks else None
                action = self._duplicate_action(existing) if existing else "new"
                if action == "cancel":
                    continue
                if action == "overwrite" and existing is not None:
                    draft = ProjectDraft(
                        **{
                            **draft.__dict__,
                            "project_code": existing.project_code,
                        }
                    )
                    self.repository.update_project(
                        existing.id,
                        self.actor,
                        draft,
                    )
                else:
                    self.repository.create_project(self.actor, draft)
                created += 1
        except Exception as exc:
            QMessageBox.warning(self, "创建失败", str(exc))
            self._scan_directory()
            return
        QMessageBox.information(
            self,
            "创建完成",
            f"已处理 {created} 个总任务。",
        )
        self._scan_directory()

    def _row_for_code(self, code: str) -> int:
        for row in range(self.product_table.rowCount()):
            item = self.product_table.item(row, 0)
            if item.data(Qt.ItemDataRole.UserRole) == code:
                return row
        return 0

    def _duplicate_action(self, existing) -> str:
        tasks = self.repository.list_project_tasks(existing.id)
        can_overwrite = all(
            task.status == "available"
            for task in tasks
        )
        box = QMessageBox(self)
        box.setWindowTitle("发现重复货号")
        text = (
            f"货号 {existing.product_code} 已有任务 {existing.project_code}。"
        )
        if can_overwrite:
            text += "\n可以覆盖现有未领取任务，或创建一个新任务。"
        else:
            text += "\n现有任务已经开始处理，不能覆盖，只能创建新任务。"
        box.setText(text)
        overwrite = box.addButton("覆盖", QMessageBox.ButtonRole.AcceptRole)
        create_new = box.addButton("新建", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        if not can_overwrite:
            overwrite.setEnabled(False)
        box.exec()
        clicked = box.clickedButton()
        if clicked is overwrite:
            return "overwrite"
        if clicked is create_new:
            return "new"
        if clicked is cancel:
            return "cancel"
        return "cancel"


def _unique_destination(folder: Path, source: Path) -> Path:
    destination = folder / source.name
    sequence = 2
    while destination.exists():
        destination = folder / f"{source.stem}_{sequence}{source.suffix}"
        sequence += 1
    return destination
