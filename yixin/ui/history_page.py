from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..constants import (
    MODE_LABELS,
    MODEL_LABELS,
    STATUS_LABELS,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCESS,
)
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import AuthUser
from ..database import Database
from ..library import LibraryService
from ..local_export import LocalImageExporter
from ..settings import AUTO_STRATEGY_LABELS, AppSettings
from ..time_utils import format_task_elapsed
from ..worker import TaskWorker
from .save_helpers import LocalSaveController, show_saved_path, show_saved_toast
from .project_history_page import ProjectHistoryPage
from .widgets import ImageStrip, ImageView, TaskImageAsset, build_task_assets

TASK_DATA_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class HistoryTabsPage(QWidget):
    library_changed = Signal()
    retry_requested = Signal(int)

    def __init__(
        self,
        database: Database,
        library_service: LibraryService,
        worker: TaskWorker,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.ordinary_page = HistoryPage(
            database,
            library_service,
            worker,
            auto_load=False,
        )
        self.project_page = ProjectHistoryPage(
            repository,
            resolver,
            actor,
            auto_load=False,
        )
        self.tabs.addTab(self.ordinary_page, "普通创作")
        self.tabs.addTab(self.project_page, "项目任务")
        layout.addWidget(self.tabs)
        self.ordinary_page.library_changed.connect(
            self.library_changed.emit
        )
        self.ordinary_page.retry_requested.connect(
            self.retry_requested.emit
        )

    def refresh(self) -> None:
        self.ordinary_page.refresh()
        self.project_page.refresh()


class HistoryPage(QWidget):
    library_changed = Signal()
    retry_requested = Signal(int)

    def __init__(
        self,
        database: Database,
        library_service: LibraryService,
        worker: TaskWorker,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.library_service = library_service
        self.worker = worker
        self.app_settings = AppSettings(database)
        self.local_save_controller = LocalSaveController(
            self,
            LocalImageExporter(database, self.app_settings),
            self.app_settings,
        )
        self.current_task_id: int | None = None
        self.current_task_snapshot: dict | None = None
        self.current_results: list[dict] = []
        self.current_assets: list[TaskImageAsset] = []

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        list_card = QFrame()
        list_card.setObjectName("Card")
        list_layout = QVBoxLayout(list_card)
        filter_row = QHBoxLayout()
        title = QLabel("历史记录")
        title.setStyleSheet("font-size:21px;font-weight:700;")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索提示词")
        self.status_filter = QComboBox()
        self.status_filter.addItem("全部状态", None)
        for status, label in STATUS_LABELS.items():
            self.status_filter.addItem(label, status)
        refresh = QPushButton("刷新")
        filter_row.addWidget(title)
        filter_row.addStretch(1)
        filter_row.addWidget(self.search_edit, 1)
        filter_row.addWidget(self.status_filter)
        filter_row.addWidget(refresh)
        list_layout.addLayout(filter_row)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["ID", "提交时间", "状态", "总耗时", "模式 / 模型", "提示词"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 118)
        self.table.setColumnWidth(2, 52)
        self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 124)
        self.table.horizontalHeader().setStretchLastSection(True)
        list_layout.addWidget(self.table, 1)

        detail_card = QFrame()
        detail_card.setObjectName("Card")
        detail_layout = QVBoxLayout(detail_card)
        self.detail_title = QLabel("选择一个历史任务")
        self.detail_title.setStyleSheet("font-size:20px;font-weight:700;")
        detail_layout.addWidget(self.detail_title)
        self.detail_meta = QLabel("任务参数和请求 ID 会显示在这里。")
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setStyleSheet("color:#637086;")
        detail_layout.addWidget(self.detail_meta)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.error_label.setStyleSheet(
            "color:#b72f3c;background:#fff5f6;border:1px solid #e9b6bb;"
            "border-radius:6px;padding:6px 8px;"
        )
        self.error_label.hide()
        detail_layout.addWidget(self.error_label)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setStyleSheet(
            "color:#2E64C4;font-weight:600;padding-top:2px;"
        )
        detail_layout.addWidget(self.elapsed_label)
        self.prompt_view = QTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setMaximumHeight(150)
        detail_layout.addWidget(self.prompt_view)
        self.asset_strip = ImageStrip(icon_size=76)
        detail_layout.addWidget(self.asset_strip)
        self.preview = ImageView()
        detail_layout.addWidget(self.preview, 1)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("取消任务")
        self.retry_button = QPushButton("重试任务")
        self.save_local_button = QPushButton("保存到本地")
        self.save_library_button = QPushButton("保存到作品库")
        self.save_library_button.setObjectName("PrimaryButton")
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.retry_button)
        actions.addStretch(1)
        actions.addWidget(self.save_local_button)
        actions.addWidget(self.save_library_button)
        detail_layout.addLayout(actions)

        splitter.addWidget(list_card)
        splitter.addWidget(detail_card)
        splitter.setSizes([570, 780])

        refresh.clicked.connect(self.refresh)
        self.search_edit.textChanged.connect(self.refresh)
        self.status_filter.currentIndexChanged.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self._selected_row_changed)
        self.asset_strip.currentRowChanged.connect(self._show_asset)
        self.cancel_button.clicked.connect(self._cancel_current)
        self.retry_button.clicked.connect(self._retry_current)
        self.save_local_button.clicked.connect(self._save_local_result)
        self.save_library_button.clicked.connect(self._save_library_result)
        self.worker.signals.task_changed.connect(self._on_worker_task_changed)
        self._set_actions(False)
        if auto_load:
            self.refresh()
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self._update_elapsed_displays)
        self.elapsed_timer.start()

    def refresh(self) -> None:
        selected_id = self.current_task_id
        search = self.search_edit.text().strip().lower()
        status = self.status_filter.currentData()
        tasks = self.database.list_tasks(limit=500)
        tasks = [
            task
            for task in tasks
            if (not status or task["status"] == status)
            and (not search or search in task["prompt"].lower())
        ]
        self.table.setRowCount(len(tasks))
        selected_row = -1
        for row, task in enumerate(tasks):
            self._set_task_row(row, task)
            if task["id"] == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.table.selectRow(selected_row)

    def _task_row_values(self, task: dict) -> list[str]:
        model_name = MODEL_LABELS.get(task["model"], task["model"])
        if task["auto_strategy"]:
            model_name += " · 自动"
        return [
            str(task["id"]),
            task["created_at"].replace("T", " ")[:16],
            STATUS_LABELS.get(task["status"], task["status"]),
            format_task_elapsed(task),
            f"{MODE_LABELS.get(task['mode'], task['mode'])} · "
            + model_name,
            task["prompt"].replace("\n", " ")[:80],
        ]

    def _set_task_row(self, row: int, task: dict) -> None:
        for column, value in enumerate(self._task_row_values(task)):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, task["id"])
                item.setData(TASK_DATA_ROLE, task)
            if column == 2:
                item.setForeground(
                    {
                        TASK_SUCCESS: QColor("#1f7a4d"),
                        TASK_FAILED: QColor("#b72f3c"),
                        TASK_RUNNING: QColor("#245ab8"),
                        TASK_CANCELLED: QColor("#7d8797"),
                    }.get(task["status"], QColor("#243044"))
                )
            self.table.setItem(row, column, item)

    def _update_elapsed_displays(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            task = item.data(TASK_DATA_ROLE) if item else None
            if task:
                elapsed_item = self.table.item(row, 3)
                if elapsed_item is None:
                    self.table.setItem(
                        row, 3, QTableWidgetItem(format_task_elapsed(task))
                    )
                else:
                    elapsed_item.setText(format_task_elapsed(task))
        if self.current_task_snapshot:
            self.elapsed_label.setText(
                f"总耗时：{format_task_elapsed(self.current_task_snapshot)}"
            )
        else:
            self.elapsed_label.clear()

    def _find_task_row(self, task_id: int) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and int(item.data(Qt.ItemDataRole.UserRole)) == task_id:
                return row
        return -1

    def _on_worker_task_changed(self, task_id: int, status: str) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            return
        search = self.search_edit.text().strip().lower()
        status_filter = self.status_filter.currentData()
        visible = (
            (not search or search in task["prompt"].lower())
            and (not status_filter or task["status"] == status_filter)
        )
        row = self._find_task_row(task_id)
        if not visible or row < 0:
            self.refresh()
        else:
            self._set_task_row(row, task)
        if self.current_task_id == task_id:
            self.show_task(task_id)

    def _selected_row_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.current_task_id = None
            self.current_task_snapshot = None
            self.elapsed_label.clear()
            self.error_label.clear()
            self.error_label.setToolTip("")
            self.error_label.hide()
            return
        item = self.table.item(rows[0].row(), 0)
        if item:
            self.show_task(int(item.data(Qt.ItemDataRole.UserRole)))

    def show_task(self, task_id: int) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            return
        self.current_task_id = task_id
        self.current_task_snapshot = task
        self.detail_title.setText(
            f"任务 #{task_id} · {STATUS_LABELS.get(task['status'], task['status'])}"
        )
        model_name = MODEL_LABELS.get(task["model"], task["model"])
        meta = [
            f"{MODE_LABELS.get(task['mode'], task['mode'])} · {model_name}",
            f"宽高比 {task['aspect_ratio']} · 分辨率 {task['resolution'] or '自动'} · 数量 {task['n']}",
            f"创建时间 {task['created_at'].replace('T', ' ')[:19]}",
        ]
        if task["auto_strategy"]:
            meta.append(
                "自动策略："
                + AUTO_STRATEGY_LABELS.get(task["auto_strategy"], task["auto_strategy"])
            )
        if task["auto_reason"]:
            meta.append(f"判断：{task['auto_reason']}")
        if task["request_id"]:
            meta.append(f"X-Request-Id：{task['request_id']}")
        if task["remote_task_id"]:
            meta.append(f"任务 ID：{task['remote_task_id']}")
        if task["input_tokens"] is not None or task["output_tokens"] is not None:
            meta.append(
                f"Token 用量：输入 {task['input_tokens'] or 0} / 输出 {task['output_tokens'] or 0}"
            )
        self.detail_meta.setText("\n".join(meta))
        self.elapsed_label.setText(f"总耗时：{format_task_elapsed(task)}")
        self.prompt_view.setPlainText(task["prompt"])
        error_message = task["error_message"] or ""
        if error_message:
            self.error_label.setText(f"错误：{error_message}")
            self.error_label.setToolTip(error_message)
            self.error_label.show()
        else:
            self.error_label.clear()
            self.error_label.setToolTip("")
            self.error_label.hide()
        self.current_results = self.database.get_task_results(task_id)
        references = self.database.get_task_references(task_id)
        self.current_assets = build_task_assets(references, self.current_results)
        self.asset_strip.set_images(
            [
                (asset.thumbnail_path, asset.label, asset)
                for asset in self.current_assets
            ]
        )
        self.cancel_button.setEnabled(task["status"] in (TASK_QUEUED, TASK_RUNNING))
        self.retry_button.setEnabled(
            task["status"] in (TASK_SUCCESS, TASK_FAILED, TASK_CANCELLED)
        )
        if self.current_assets:
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
            self._show_asset(default_row)
        else:
            self.save_local_button.setEnabled(False)
            self.save_library_button.setEnabled(False)
            self.preview.set_image(None)
            self.preview.set_placeholder(task["error_message"] or "没有生成图片")

    def _set_actions(self, enabled: bool) -> None:
        self.cancel_button.setEnabled(enabled)
        self.retry_button.setEnabled(enabled)
        self.save_local_button.setEnabled(enabled)
        self.save_library_button.setEnabled(enabled)

    def _show_asset(self, row: int) -> None:
        if not 0 <= row < len(self.current_assets):
            return
        asset = self.current_assets[row]
        self.preview.set_image(asset.path)
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

    def _cancel_current(self) -> None:
        if self.current_task_id is None:
            return
        task = self.database.get_task(self.current_task_id)
        if task is None:
            return
        if task["status"] == TASK_RUNNING:
            answer = QMessageBox.warning(
                self,
                "停止等待",
                "停止等待不会保证服务端终止请求，仍可能产生费用。\n是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.worker.request_cancel(self.current_task_id)
        self.refresh()
        self.show_task(self.current_task_id)

    def _retry_current(self) -> None:
        if self.current_task_id is None:
            return
        self.retry_requested.emit(self.current_task_id)

    def _save_local_result(self) -> None:
        if self.current_task_id is None:
            return
        row = self.asset_strip.currentRow()
        if not 0 <= row < len(self.current_assets):
            return
        asset = self.current_assets[row]
        if not asset.is_result:
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
        row = self.asset_strip.currentRow()
        if not 0 <= row < len(self.current_assets):
            return
        asset = self.current_assets[row]
        if not asset.is_result:
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
