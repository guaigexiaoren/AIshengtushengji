from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QImage,
    QMouseEvent,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QStyleOptionButton,
    QStyleOptionHeader,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.folders import (
    AccountDirectory,
    natural_sort_key,
    project_assets,
    resolve_account_directory,
    scan_product_folders,
)
from ..collaboration.matching import MatchAsset, build_pairs
from ..collaboration.product_code import ProductCode
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    PAIRING_ALL,
    ROLE_ADMIN,
    ROLE_AI,
    ROLE_OPERATIONS,
    AuthUser,
    PairingInput,
    ProjectDraft,
    PROJECT_SCOPE_LABELS,
)
from ..storage import SUPPORTED_IMAGE_SUFFIXES, atomic_copy_file, image_metadata
from .async_thumbnails import ThumbnailLoader, get_thumbnail_loader
from .async_queries import get_query_runner
from .components import PageActionBar, PageHeader
from .widgets import load_scaled_pixmap, scan_image_inputs


@dataclass
class ProductDraftState:
    product_code: str
    path: Path
    garments: list[Path]
    models: list[Path]
    errors: list[str] = field(default_factory=list)
    manual_pairs: list[PairingInput] = field(default_factory=list)
    existing_tasks: list = field(default_factory=list)
    code_info: ProductCode | None = None


class DroppableImageList(QListWidget):
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _local_paths(event) -> list[Path]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [
            Path(url.toLocalFile())
            for url in mime.urls()
            if url.isLocalFile() and url.toLocalFile()
        ]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._local_paths(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self._local_paths(event)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class CheckableHeaderView(QHeaderView):
    toggle_requested = Signal(Qt.CheckState)

    def __init__(
        self,
        orientation: Qt.Orientation,
        parent: QWidget | None = None,
        *,
        check_section: int = 0,
        label: str = "选择",
    ) -> None:
        super().__init__(orientation, parent)
        self.check_section = check_section
        self.label = label
        self._check_state = Qt.CheckState.Unchecked
        self.setSectionsClickable(True)

    def check_state(self) -> Qt.CheckState:
        return self._check_state

    def set_check_state(self, state: Qt.CheckState) -> None:
        if state == self._check_state:
            return
        self._check_state = state
        self.viewport().update()

    def paintSection(
        self,
        painter,
        rect: QRect,
        logical_index: int,
    ) -> None:
        if logical_index != self.check_section:
            super().paintSection(painter, rect, logical_index)
            return
        option = QStyleOptionHeader()
        self.initStyleOption(option)
        option.rect = rect
        option.text = ""
        self.style().drawControl(
            QStyle.ControlElement.CE_Header,
            option,
            painter,
            self,
        )
        checkbox = QStyleOptionButton()
        indicator_width = self.style().pixelMetric(
            QStyle.PixelMetric.PM_IndicatorWidth,
            checkbox,
            self,
        )
        checkbox.rect = QRect(
            rect.left() + 8,
            rect.center().y() - indicator_width // 2,
            indicator_width,
            indicator_width,
        )
        checkbox.state = QStyle.StateFlag.State_Enabled
        if self._check_state == Qt.CheckState.Checked:
            checkbox.state |= QStyle.StateFlag.State_On
        elif self._check_state == Qt.CheckState.PartiallyChecked:
            checkbox.state |= QStyle.StateFlag.State_NoChange
        else:
            checkbox.state |= QStyle.StateFlag.State_Off
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_IndicatorCheckBox,
            checkbox,
            painter,
            self,
        )
        painter.drawText(
            QRect(
                rect.left() + indicator_width + 14,
                rect.top(),
                rect.width() - indicator_width - 18,
                rect.height(),
            ),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.label,
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.logicalIndexAt(event.position().toPoint())
            == self.check_section
        ):
            state = (
                Qt.CheckState.Unchecked
                if self._check_state == Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )
            self.set_check_state(state)
            self.toggle_requested.emit(state)
            event.accept()
            return
        super().mousePressEvent(event)


class TaskCreatePage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
        async_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.async_mode = async_mode
        self.query_runner = (
            get_query_runner() if async_mode else None
        )
        self._active_scan_request = -1
        self.states: dict[Path, ProductDraftState] = {}
        self.current_state: ProductDraftState | None = None
        self.account_directory: AccountDirectory | None = None
        self.accessible_projects: tuple[tuple[str, str], ...] = ()
        self.pairing_mode = PAIRING_ALL
        self.thumbnail_loader = get_thumbnail_loader()
        self._thumbnail_generation = 0
        self._asset_thumbnail_contexts: dict[
            int,
            tuple[int, QListWidget, int, Path, QSize],
        ] = {}
        self.thumbnail_loader.thumbnail_ready.connect(
            self._asset_thumbnail_ready
        )
        self.thumbnail_loader.thumbnail_failed.connect(
            self._asset_thumbnail_failed
        )
        self._build()
        if self.query_runner is not None:
            self.query_runner.succeeded.connect(self._query_succeeded)
            self.query_runner.failed.connect(self._query_failed)
        if auto_load:
            self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        header = QHBoxLayout()
        page_header = PageHeader(
            "任务提交",
            "扫描共享盘货号，确认衣服与模特后创建任务。",
        )
        self.refresh_button = QPushButton("刷新扫描")
        self.refresh_button.clicked.connect(self.refresh)
        page_header.add_action(self.refresh_button)
        header.addWidget(page_header)
        outer.addLayout(header)

        path_card = QGroupBox("账号目录")
        path_layout = QVBoxLayout(path_card)
        project_row = QHBoxLayout()
        self.project_combo = QComboBox()
        self.project_combo.currentIndexChanged.connect(
            self._project_changed
        )
        project_row.addWidget(QLabel("当前项目"))
        project_row.addWidget(self.project_combo, 1)
        path_layout.addLayout(project_row)
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
        scan_actions.addStretch(1)
        self.scan_summary = QLabel()
        scan_actions.addWidget(self.scan_summary)
        scan_layout.addLayout(scan_actions)
        self.product_table = QTableWidget(0, 8)
        self.product_table.setHorizontalHeader(
            CheckableHeaderView(
                Qt.Orientation.Horizontal,
                self.product_table,
                check_section=1,
                label="",
            )
        )
        self.product_table.setHorizontalHeaderLabels(
            [
                "状态",
                "",
                "货号",
                "衣服图",
                "模特图",
                "子任务",
                "预计任务编码",
                "重复情况",
            ]
        )
        self.product_table.verticalHeader().setVisible(False)
        self.product_table.setColumnWidth(1, 42)
        self.product_table.horizontalHeader().setToolTip(
            "全选或取消全选"
        )
        self.product_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.product_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.product_table.horizontalHeader().setStretchLastSection(True)
        self.product_table.itemSelectionChanged.connect(self._product_selected)
        self.product_table.itemChanged.connect(
            self._product_check_changed
        )
        self.product_table.horizontalHeader().toggle_requested.connect(
            self._set_all_eligible
        )
        scan_layout.addWidget(self.product_table, 1)
        detail_card = QGroupBox("素材预览")
        detail_layout = QVBoxLayout(detail_card)
        mode_row = QHBoxLayout()
        self.detail_title = QLabel("选择一个货号查看图片")
        mode_row.addWidget(self.detail_title)
        detail_layout.addLayout(mode_row)

        assets_row = QHBoxLayout()
        self.garment_list, garment_box = self._asset_box("衣服图", "garment")
        self.model_list, model_box = self._asset_box("模特图", "model")
        assets_row.addWidget(garment_box, 1)
        assets_row.addWidget(model_box, 1)
        detail_layout.addLayout(assets_row)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        body.addWidget(scan_card)
        body.addWidget(detail_card)
        body.setSizes([720, 560])
        outer.addWidget(body, 1)

        action_bar = PageActionBar()
        footer = action_bar.layout
        footer.addWidget(QLabel("备注"))
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText(
            "选填，会应用到本次创建的任务"
        )
        footer.addWidget(self.note_edit, 1)
        footer.addStretch(1)
        self.urgent_checkbox = QCheckBox("设为加急")
        footer.addWidget(self.urgent_checkbox)
        self.create_button = QPushButton("创建所选任务")
        self.create_button.setObjectName("PrimaryButton")
        self.create_button.setMinimumHeight(42)
        self.create_button.clicked.connect(self._create_selected)
        self.create_button.setEnabled(
            self.actor.role in (ROLE_OPERATIONS, ROLE_AI, ROLE_ADMIN)
        )
        footer.addWidget(self.create_button)
        outer.addWidget(action_bar)

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
        images = DroppableImageList()
        images.setViewMode(QListWidget.ViewMode.IconMode)
        images.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        images.setIconSize(QSize(128, 128))
        images.setGridSize(QSize(120, 125))
        images.setResizeMode(QListWidget.ResizeMode.Adjust)
        images.setMinimumHeight(240)
        images.files_dropped.connect(
            lambda paths: self._handle_image_drop(role, paths)
        )
        layout.addWidget(images)
        return images, box

    def refresh(self) -> None:
        shared_root = self.repository.get_shared_setting("share_root")
        if shared_root:
            self.resolver = SharedPathResolver(shared_root)
        self._load_project_choices()
        project = self.project_combo.currentData()
        if not project:
            self.scan_summary.setText(
                "尚未分配项目，请联系管理员在账号编辑中分配"
            )
            self.product_table.setRowCount(0)
            self.states = {}
            self.current_state = None
            self.garment_list.clear()
            self.model_list.clear()
            self.create_button.setEnabled(False)
            return
        self.create_button.setEnabled(
            self.actor.role in (ROLE_OPERATIONS, ROLE_AI, ROLE_ADMIN)
        )
        scope, project_name = project
        project_root = (
            self.resolver.root
            / PROJECT_SCOPE_LABELS[scope]
            / project_name
        )
        raw_path = self.account_path.text().strip()
        if not raw_path:
            raw_path = str(
                project_root / f"{date.today().month}.{date.today().day}"
            )
        try:
            account_directory = resolve_account_directory(
                raw_path,
                project_root,
            )
            account_directory.path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.account_directory = None
            self.states = {}
            self.current_state = None
            self.product_table.setRowCount(0)
            self.create_button.setEnabled(False)
            QMessageBox.warning(
                self,
                "无法访问共享盘",
                f"路径：{raw_path}\n{exc}",
            )
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

    def _load_project_choices(self) -> None:
        selected = self.project_combo.currentData()
        try:
            self.accessible_projects = self.repository.list_accessible_projects(
                self.actor
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取项目失败", str(exc))
            self.accessible_projects = ()
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for scope, project_name in self.accessible_projects:
            self.project_combo.addItem(
                f"{PROJECT_SCOPE_LABELS[scope]} · {project_name}",
                (scope, project_name),
            )
        index = self.project_combo.findData(selected)
        if index < 0 and self.project_combo.count():
            index = 0
        if index >= 0:
            self.project_combo.setCurrentIndex(index)
        self.project_combo.blockSignals(False)

    def _project_changed(self) -> None:
        project = self.project_combo.currentData()
        if not project:
            return
        scope, project_name = project
        directory = (
            self.resolver.root
            / PROJECT_SCOPE_LABELS[scope]
            / project_name
            / f"{date.today().month}.{date.today().day}"
        )
        self.account_path.setText(str(directory))
        self.refresh()

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
        project = self.project_combo.currentData()
        check_product_code = bool(project) and project[0] == "internal"
        if self.async_mode:
            account_directory = self.account_directory
            self.scan_summary.setText("正在扫描共享盘货号目录…")
            self.create_button.setEnabled(False)

            def load():
                products = scan_product_folders(
                    account_directory.path,
                    check_product_code=check_product_code,
                )
                existing = {
                    product.path: (
                        self.repository.find_tasks_by_product(
                            self.actor.id,
                            account_directory.source_date,
                            product.product_code,
                        )
                    )
                    for product in products
                }
                return products, existing

            self._active_scan_request = self.query_runner.submit(load)
            return
        try:
            products = scan_product_folders(
                self.account_directory.path,
                check_product_code=check_product_code,
            )
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
            self.states[product.path] = ProductDraftState(
                product_code=product.product_code,
                path=product.path,
                garments=list(product.garments),
                models=list(product.models),
                errors=list(product.errors),
                existing_tasks=existing,
                code_info=product.code_info,
            )
        self._refresh_product_table()
        self._finish_scan_selection()

    def _query_succeeded(self, request_id: int, result) -> None:
        if (
            not self.async_mode
            or request_id != self._active_scan_request
        ):
            return
        products, existing = result
        self.states = {}
        for product in products:
            self.states[product.path] = ProductDraftState(
                product_code=product.product_code,
                path=product.path,
                garments=list(product.garments),
                models=list(product.models),
                errors=list(product.errors),
                existing_tasks=list(
                    existing.get(product.path, [])
                ),
                code_info=product.code_info,
            )
        self._refresh_product_table()
        self._finish_scan_selection()

    def _query_failed(self, request_id: int, message: str) -> None:
        if (
            not self.async_mode
            or request_id != self._active_scan_request
        ):
            return
        self.scan_summary.setText("共享盘扫描失败")
        self.create_button.setEnabled(False)
        QMessageBox.warning(self, "扫描失败", message)

    def _finish_scan_selection(self) -> None:
        self.create_button.setEnabled(
            self.actor.role in (ROLE_OPERATIONS, ROLE_AI, ROLE_ADMIN)
            and bool(self.states)
        )
        if self.product_table.rowCount():
            self.product_table.selectRow(0)
        else:
            self.current_state = None
            self.detail_title.setText("当前日期没有符合结构的货号目录")
            self.garment_list.clear()
            self.model_list.clear()

    def _refresh_product_table(self) -> None:
        self.product_table.blockSignals(True)
        self.product_table.setRowCount(0)
        selectable = 0
        unrecognized = 0
        project = self.project_combo.currentData()
        check_product_code = bool(project) and project[0] == "internal"
        for state in sorted(
            self.states.values(),
            key=lambda item: (
                item.product_code.casefold(),
                str(item.path).casefold(),
            ),
        ):
            row = self.product_table.rowCount()
            self.product_table.insertRow(row)
            check = QTableWidgetItem()
            can_select = not state.errors and not state.existing_tasks
            check.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | (
                    Qt.ItemFlag.ItemIsUserCheckable
                    if can_select
                    else Qt.ItemFlag.NoItemFlags
                )
            )
            check.setCheckState(
                Qt.CheckState.Checked if can_select else Qt.CheckState.Unchecked
            )
            if can_select:
                selectable += 1
            code_invalid = check_product_code and state.code_info is None
            if code_invalid:
                unrecognized += 1
            if code_invalid:
                status_text = "货号不符"
            elif can_select:
                status_text = "可创建"
            else:
                status_text = "需处理"
            status_item = QTableWidgetItem(status_text)
            if state.errors:
                status_item.setToolTip("；".join(state.errors))
            self.product_table.setItem(
                row,
                0,
                status_item,
            )
            self.product_table.setItem(row, 1, check)
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
            )
            for column, value in enumerate(values, start=2):
                self.product_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )
            self.product_table.item(row, 1).setData(
                Qt.ItemDataRole.UserRole,
                str(state.path),
            )
        summary = f"共 {len(self.states)} 个货号，可直接创建 {selectable} 个"
        if unrecognized:
            summary += (
                f"；{unrecognized} 个货号格式不符，未识别（仅提示）"
            )
        self.scan_summary.setText(summary)
        self.product_table.blockSignals(False)
        self._update_select_header()

    def _product_check_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 1:
            self._update_select_header()

    def _update_select_header(self) -> None:
        checked = 0
        eligible = 0
        for row in range(self.product_table.rowCount()):
            item = self.product_table.item(row, 1)
            if item is None or not (
                item.flags() & Qt.ItemFlag.ItemIsUserCheckable
            ):
                continue
            eligible += 1
            if item.checkState() == Qt.CheckState.Checked:
                checked += 1
        if not eligible or checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked == eligible:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self.product_table.horizontalHeader().set_check_state(state)

    def _set_all_eligible(self, state: Qt.CheckState) -> None:
        checked = state == Qt.CheckState.Checked
        self.product_table.blockSignals(True)
        for row in range(self.product_table.rowCount()):
            item = self.product_table.item(row, 1)
            if item is None or not (
                item.flags() & Qt.ItemFlag.ItemIsUserCheckable
            ):
                continue
            item.setCheckState(
                Qt.CheckState.Checked
                if checked
                else Qt.CheckState.Unchecked
            )
        self.product_table.blockSignals(False)
        self._update_select_header()

    def _selected_state_key(self) -> Path | None:
        row = self.product_table.currentRow()
        if row < 0:
            return None
        item = self.product_table.item(row, 1)
        if item is None:
            return None
        raw = item.data(Qt.ItemDataRole.UserRole)
        return Path(raw) if raw else None

    def _product_selected(self) -> None:
        key = self._selected_state_key()
        self.current_state = self.states.get(key) if key else None
        self._show_state()

    def _show_state(self) -> None:
        state = self.current_state
        self._thumbnail_generation += 1
        self._asset_thumbnail_contexts.clear()
        self.garment_list.clear()
        self.model_list.clear()
        if state is None:
            return
        self.detail_title.setText(
            f"{state.product_code} · 衣服 {len(state.garments)} 张 · "
            f"模特 {len(state.models)} 张"
        )
        self._fill_assets(self.garment_list, state.garments)
        self._fill_assets(self.model_list, state.models)

    def _fill_assets(
        self,
        widget: QListWidget,
        paths: list[Path],
    ) -> None:
        for path in paths:
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            widget.addItem(item)
            target = QSize(96, 96)
            request = self.thumbnail_loader.request_thumbnail(
                path,
                target,
            )
            self._asset_thumbnail_contexts[request.request_id] = (
                self._thumbnail_generation,
                widget,
                widget.count() - 1,
                path,
                target,
            )

    def _asset_thumbnail_ready(
        self,
        request_id: int,
        _path: str,
        image: QImage,
    ) -> None:
        context = self._asset_thumbnail_contexts.pop(request_id, None)
        if context is None:
            return
        generation, widget, row, expected_path, target = context
        if generation != self._thumbnail_generation:
            return
        item = widget.item(row)
        if (
            item is None
            or item.data(Qt.ItemDataRole.UserRole) != str(expected_path)
        ):
            return
        scaled = image.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        item.setIcon(QIcon(QPixmap.fromImage(scaled)))

    def _asset_thumbnail_failed(
        self,
        request_id: int,
        _path: str,
        _message: str,
    ) -> None:
        self._asset_thumbnail_contexts.pop(request_id, None)

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
                if role == "garment" and len(target) >= 13:
                    errors.append("衣服图最多 13 张，超出部分未导入")
                    break
                data = source.read_bytes()
                image_metadata(data)
                if source.resolve().parent == role_folder.resolve():
                    destination = source.resolve()
                else:
                    destination = _unique_destination(role_folder, source)
                    atomic_copy_file(source.resolve(), destination)
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

    def _handle_image_drop(self, role: str, raw_paths: list) -> None:
        if self.current_state is None:
            QMessageBox.information(
                self,
                "请选择货号",
                "请先在列表中选择一个货号。",
            )
            return
        paths, errors = scan_image_inputs(
            [Path(path) for path in raw_paths]
        )
        if errors:
            QMessageBox.warning(
                self,
                "部分图片未导入",
                "\n".join(errors[:8]),
            )
        self._import_paths(role, paths)

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
            self.pairing_mode,
            current.manual_pairs,
        )

    def _selected_keys(self) -> list[Path]:
        keys: list[Path] = []
        for row in range(self.product_table.rowCount()):
            item = self.product_table.item(row, 1)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            raw = item.data(Qt.ItemDataRole.UserRole)
            if raw:
                keys.append(Path(raw))
        return keys

    def _create_selected(self) -> None:
        keys = self._selected_keys()
        if not keys:
            QMessageBox.warning(self, "未选择货号", "请至少勾选一个货号。")
            return
        account_directory = self.account_directory
        if account_directory is None:
            return
        selected_project = self.project_combo.currentData()
        if not selected_project:
            QMessageBox.warning(
                self,
                "缺少项目",
                "请先选择管理员分配的项目。",
            )
            return
        project_scope, project_name = selected_project
        is_urgent = self.urgent_checkbox.isChecked()
        if is_urgent:
            new_urgent_count = sum(
                1
                for key in keys
                if not (
                    self.states[key].existing_tasks
                    and self.states[key].existing_tasks[0].is_urgent
                )
            )
            remaining = self.repository.remaining_urgent_quota(
                self.actor,
                date.today(),
            )
            if remaining < new_urgent_count:
                QMessageBox.warning(
                    self,
                    "加急额度不足",
                    f"本次需要新增 {new_urgent_count} 个加急主任务，"
                    f"今日仅剩 {remaining} 个加急额度。",
                )
                return
        answer = QMessageBox.question(
            self,
            "确认创建",
            f"确认创建 {len(keys)} 个总任务吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        validated: list[tuple[ProductDraftState, object]] = []
        for key in keys:
            state = self.states[key]
            if state.errors:
                self.product_table.selectRow(self._row_for_key(key))
                QMessageBox.warning(
                    self,
                    "货号资料不完整",
                    f"{state.product_code}：{'；'.join(state.errors)}",
                )
                return
            if not state.garments or not state.models:
                self.product_table.selectRow(self._row_for_key(key))
                QMessageBox.warning(
                    self,
                    "缺少素材",
                    f"{state.product_code}：衣服图和模特图不能为空",
                )
                return
            if len(state.garments) > 13:
                self.product_table.selectRow(self._row_for_key(key))
                QMessageBox.warning(
                    self,
                    "衣服图过多",
                    f"{state.product_code}：单个模特任务最多 13 张衣服图",
                )
                return
            result = self._current_pairs(state)
            if result is None or result.errors:
                self.product_table.selectRow(self._row_for_key(key))
                QMessageBox.warning(
                    self,
                    "配对失败",
                    f"{state.product_code}："
                    + "；".join(
                        result.errors if result else ["配对失败"]
                    ),
                )
                return
            validated.append((state, result))
        created = 0
        try:
            for state, result in validated:
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
                    pairing_mode=self.pairing_mode,
                    assets=assets,
                    pairs=result.pairs,
                    note=self.note_edit.text().strip(),
                    source_date=account_directory.source_date,
                    product_code=state.product_code,
                    project_scope=project_scope,
                    access_project_name=project_name,
                    is_urgent=is_urgent,
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
                    self.repository.set_project_urgent(
                        existing.id,
                        self.actor,
                        is_urgent,
                    )
                else:
                    self.repository.create_project(
                        self.actor,
                        draft,
                    )
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

    def _row_for_key(self, key: Path) -> int:
        for row in range(self.product_table.rowCount()):
            item = self.product_table.item(row, 1)
            if item.data(Qt.ItemDataRole.UserRole) == str(key):
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
