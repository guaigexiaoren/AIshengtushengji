from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    ATTEMPT_CANCELLED,
    ATTEMPT_FAILED,
    ATTEMPT_QUEUED,
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    ROLE_ADMIN,
    AuthUser,
)
from ..constants import (
    MODE_DRESS_UP,
    MODE_LABELS,
    MODE_FREE_COMPOSE,
    MODEL_LABELS,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCESS,
)
from ..database import Database
from ..library import LibraryService
from ..local_export import export_original_image
from ..settings import (
    AUTO_STRATEGY_LABELS,
    LOCAL_SAVE_FIXED,
    LOCAL_SAVE_REFERENCE,
    AppSettings,
)
from ..storage import StoredImage
from ..time_utils import format_task_elapsed, utc_naive_to_local
from ..worker import TaskWorker
from ..paging import HistoryQuery, PageRequest, TaskQuery
from .async_queries import get_query_runner
from .components import FilterBar, PageActionBar, PageHeader
from .date_filter import OptionalDateEdit
from .image_preview import DualImagePreview
from .pagination_bar import PaginationBar
from .save_helpers import show_saved_path, show_saved_toast
from .widgets import build_task_assets


HISTORY_KEY_ROLE = int(Qt.ItemDataRole.UserRole) + 10
HISTORY_RECORD_ROLE = int(Qt.ItemDataRole.UserRole) + 11


STATUS_LABELS = {
    TASK_QUEUED: "排队中",
    ATTEMPT_QUEUED: "排队中",
    TASK_RUNNING: "生成中",
    ATTEMPT_RUNNING: "生成中",
    TASK_SUCCESS: "成功",
    ATTEMPT_SUCCEEDED: "成功",
    TASK_FAILED: "失败",
    ATTEMPT_FAILED: "失败",
    TASK_CANCELLED: "已取消",
    ATTEMPT_CANCELLED: "已取消",
    "manual_ps": "PS成品",
}


@dataclass(frozen=True)
class HistoryRecord:
    kind: str
    record_id: int
    task_id: int
    attempt_id: int | None
    status: str
    created_at: datetime
    project_name: str
    product_code: str
    main_code: str
    secondary_code: str
    operator_name: str
    model: str
    provider_name: str
    prompt: str
    result_count: int
    raw: object

    @property
    def kind_label(self) -> str:
        return "项目" if self.kind == "project" else "普通"

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.record_id}"


class UnifiedHistoryPage(QWidget):
    library_changed = Signal()
    retry_requested = Signal(int)
    project_retry_requested = Signal(int, int)
    project_attempt_cancelled = Signal(int)

    def __init__(
        self,
        database: Database,
        library_service: LibraryService,
        worker: TaskWorker,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
        async_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.library_service = library_service
        self.worker = worker
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.async_mode = async_mode
        self.app_settings = AppSettings(database)
        self.query_runner = (
            get_query_runner() if self.async_mode else None
        )
        self._active_request = -1
        self._active_detail_request = -1
        self._project_names: list[str] = []
        self._page_request = PageRequest()
        self._sort_field = "created_at"
        self._sort_direction = "desc"
        self._keyword_timer = QTimer(self)
        self._keyword_timer.setSingleShot(True)
        self._keyword_timer.setInterval(350)
        self._keyword_timer.timeout.connect(self._request_history_page)
        self.records: list[HistoryRecord] = []
        self._records_by_key: dict[str, HistoryRecord] = {}
        self.current_record: HistoryRecord | None = None
        self.current_assets: list = []
        self.current_selected_asset = None
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
        outer.addWidget(
            PageHeader(
                "历史记录",
                "统一查看项目生成轮次和普通创作，并保存或重试结果。",
            )
        )
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QFrame()
        left.setObjectName("Card")
        left_layout = QVBoxLayout(left)
        filter_bar = FilterBar()
        filters = filter_bar.layout
        self.project_filter = QComboBox()
        self.project_filter.addItem("全部项目", None)
        self.product_search = QLineEdit()
        self.product_search.setPlaceholderText("搜索货号")
        self.status_filter = QComboBox()
        self.status_filter.addItem("全部状态", None)
        for value, label in (
            (ATTEMPT_QUEUED, "排队中"),
            (ATTEMPT_RUNNING, "生成中"),
            (ATTEMPT_SUCCEEDED, "成功"),
            (ATTEMPT_FAILED, "失败"),
            (ATTEMPT_CANCELLED, "已取消"),
            ("manual_ps", "PS成品"),
        ):
            self.status_filter.addItem(label, value)
        self.type_filter = QComboBox()
        self.type_filter.addItem("全部类型", None)
        self.type_filter.addItem("项目", "project")
        self.type_filter.addItem("普通", "ordinary")
        self.start_date = OptionalDateEdit("开始日期")
        self.end_date = OptionalDateEdit("结束日期")
        reset = QPushButton("重置筛选")
        refresh = QPushButton("刷新")
        filters.addWidget(self.project_filter, 1)
        filters.addWidget(self.product_search, 1)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.type_filter)
        filters.addWidget(self.start_date)
        filters.addWidget(self.end_date)
        filters.addWidget(reset)
        filters.addWidget(refresh)
        left_layout.addWidget(filter_bar)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "任务类型",
                "项目 / 任务",
                "子任务 / 模式",
                "货号 / 标题",
                "操作人员",
                "模型",
                "状态",
                "创建时间",
                "结果数",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate(
            (78, 160, 160, 95, 80, 95, 70, 115, 50)
        ):
            self.table.setColumnWidth(column, width)
        left_layout.addWidget(self.table, 1)
        self.pagination = PaginationBar()
        self.pagination.page_requested.connect(self._page_requested)
        left_layout.addWidget(self.pagination)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("Card")
        right_layout = QVBoxLayout(right)
        self.detail_title = QLabel("选择一条历史记录")
        self.detail_title.setStyleSheet(
            "font-size:18px;font-weight:700;"
        )
        self.detail_meta = QLabel()
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setStyleSheet("color:#60708a;")
        self.prompt_view = QTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setMaximumHeight(145)
        right_layout.addWidget(self.detail_title)
        right_layout.addWidget(self.detail_meta)
        right_layout.addWidget(self.prompt_view)

        self.dual_preview = DualImagePreview(
            self.resolver,
            async_thumbnails=self.async_mode,
        )
        self.dual_preview.selection_changed.connect(
            self._preview_selected
        )
        self.result_gallery = self.dual_preview.result_pane
        self.reference_gallery = self.dual_preview.reference_pane
        self.reference_strip = self.reference_gallery.list_widget
        self.preview = self.dual_preview.result_pane.preview
        right_layout.addWidget(self.dual_preview, 1)
        self.cancel_button = QPushButton("取消任务")
        self.retry_button = QPushButton("重试任务")
        self.save_local_button = QPushButton("保存本地")
        self.save_local_button.setStyleSheet(
            "QPushButton { background:#2e64c4;color:white;"
            "border:1px solid #2455aa;border-radius:5px;"
            "padding:6px 12px;font-weight:600; }"
            "QPushButton:disabled { background:#aebad0;color:#eef2f8; }"
        )
        self.save_library_button = QPushButton("保存作品库")
        splitter.addWidget(right)
        splitter.setSizes([900, 600])
        outer.addWidget(splitter, 1)
        action_bar = PageActionBar()
        action_bar.layout.addWidget(self.cancel_button)
        action_bar.layout.addWidget(self.retry_button)
        action_bar.layout.addStretch(1)
        action_bar.layout.addWidget(self.save_local_button)
        action_bar.layout.addWidget(self.save_library_button)
        self.page_action_bar = action_bar
        outer.addWidget(action_bar)
        refresh.clicked.connect(self.refresh)
        reset.clicked.connect(self._reset_filters)
        self.project_filter.currentIndexChanged.connect(self._filter_changed)
        self.product_search.textChanged.connect(self._filter_changed)
        self.status_filter.currentIndexChanged.connect(self._filter_changed)
        self.type_filter.currentIndexChanged.connect(self._filter_changed)
        self.start_date.dateChanged.connect(self._filter_changed)
        self.end_date.dateChanged.connect(self._filter_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.cancel_button.clicked.connect(self._cancel_current)
        self.retry_button.clicked.connect(self._retry_current)
        self.save_local_button.clicked.connect(self._save_local)
        self.save_library_button.clicked.connect(self._save_library)
        self._set_actions(False)
        if self.async_mode:
            self.table.setSortingEnabled(False)
            self.table.horizontalHeader().setSortIndicatorShown(True)
            self.table.horizontalHeader().setSortIndicator(
                7,
                Qt.SortOrder.DescendingOrder,
            )
            self.table.horizontalHeader().sectionClicked.connect(
                self._sort_requested
            )

    def refresh(self) -> None:
        if self.async_mode:
            self._request_history_page()
            return
        shared_root = self.repository.get_shared_setting("share_root")
        if shared_root:
            self.resolver = SharedPathResolver(shared_root)
            self.dual_preview.set_resolver(self.resolver)
        project_rows = []
        try:
            project_rows = self.repository.list_project_attempt_history(
                self.actor
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取项目历史失败", str(exc))
        ordinary_tasks = self.database.list_tasks(limit=5000)
        records = [
            self._project_record(row) for row in project_rows
        ]
        records.extend(
            self._ordinary_record(task) for task in ordinary_tasks
        )
        self.records = sorted(
            records,
            key=lambda item: item.created_at,
            reverse=True,
        )
        self._refresh_project_filter()
        self._apply_filters()

    def _history_query(self) -> HistoryQuery:
        return HistoryQuery(
            keyword=self.product_search.text().strip(),
            project_name=self.project_filter.currentData(),
            status=self.status_filter.currentData(),
            task_type=self.type_filter.currentData(),
            start_date=self.start_date.python_date(),
            end_date=self.end_date.python_date(),
        )

    @staticmethod
    def _local_statuses(status: str | None) -> tuple[str, ...]:
        mapping = {
            ATTEMPT_QUEUED: TASK_QUEUED,
            ATTEMPT_RUNNING: TASK_RUNNING,
            ATTEMPT_SUCCEEDED: TASK_SUCCESS,
            ATTEMPT_FAILED: TASK_FAILED,
            ATTEMPT_CANCELLED: TASK_CANCELLED,
        }
        mapped = mapping.get(status or "")
        return (mapped,) if mapped else ()

    def _request_history_page(self) -> None:
        self._active_detail_request = -1
        self._set_actions(False)
        query = self._history_query()
        sort_page = PageRequest(
            page=self._page_request.page,
            page_size=100,
            sort_field=self._sort_field,
            sort_direction=self._sort_direction,
        )
        fetch_page = PageRequest(
            page=1,
            page_size=sort_page.page * sort_page.page_size,
            sort_field=self._sort_field,
            sort_direction=self._sort_direction,
        )
        self.detail_title.setText("正在加载历史记录…")

        def load():
            project_result = None
            ordinary_result = None
            project_names = list(self._project_names)
            if query.task_type != "ordinary":
                project_result = (
                    self.repository.list_project_attempt_history_page(
                        self.actor,
                        query,
                        fetch_page,
                    )
                )
                if not project_names:
                    project_names = (
                        self.repository.list_access_project_names(
                            self.actor
                        )
                    )
            if (
                query.task_type != "project"
                and query.status != "manual_ps"
            ):
                ordinary_result = self.database.list_tasks_page(
                    TaskQuery(
                        keyword=query.keyword,
                        statuses=self._local_statuses(query.status),
                        start_date=query.start_date,
                        end_date=query.end_date,
                    ),
                    fetch_page,
                )
            return query, project_result, ordinary_result, project_names

        self._active_request = self.query_runner.submit(load)

    def _query_succeeded(self, request_id: int, result) -> None:
        if not self.async_mode:
            return
        if request_id == self._active_detail_request:
            record = self.current_record
            if record is None:
                return
            assets, model_reference_path, meta = result
            self._render_record_detail(
                record,
                assets,
                model_reference_path,
                meta,
            )
            return
        if request_id != self._active_request:
            return
        query, project_result, ordinary_result, project_names = result
        if project_names:
            self._project_names = list(project_names)
        records = []
        total = 0
        if project_result is not None:
            records.extend(
                self._project_record(row)
                for row in project_result.items
            )
            total += project_result.total
        if ordinary_result is not None:
            records.extend(
                self._ordinary_record(task)
                for task in ordinary_result.items
            )
            total += ordinary_result.total
        total_pages = max(
            1,
            (total + self._page_request.page_size - 1)
            // self._page_request.page_size,
        )
        if self._page_request.page > total_pages:
            self._page_request = PageRequest(
                page=total_pages,
                page_size=100,
                sort_field=self._sort_field,
                sort_direction=self._sort_direction,
            )
            self._request_history_page()
            return
        reverse = self._sort_direction == "desc"

        def sort_key(record: HistoryRecord):
            values = {
                "kind": record.kind_label,
                "main_code": record.main_code,
                "secondary_code": record.secondary_code,
                "product_code": record.product_code,
                "operator_name": record.operator_name,
                "model": record.model,
                "status": record.status_label,
                "created_at": record.created_at,
                "result_count": record.result_count,
            }
            primary = values.get(self._sort_field, record.created_at)
            return primary, record.created_at, record.key

        records.sort(key=sort_key, reverse=reverse)
        start = (self._page_request.page - 1) * self._page_request.page_size
        end = start + self._page_request.page_size
        page_records = records[start:end]
        self._render_history_page(page_records, total)
        self._set_project_filter_names(project_names)

    def _query_failed(self, request_id: int, message: str) -> None:
        if not self.async_mode or request_id not in (
            self._active_request,
            self._active_detail_request,
        ):
            return
        self.detail_title.setText("历史记录加载失败")
        QMessageBox.warning(self, "读取历史记录失败", message)
        if self.current_record is not None:
            self._update_actions(self.current_record)

    def _render_history_page(
        self,
        records: list[HistoryRecord],
        total: int,
    ) -> None:
        current_key = (
            self.current_record.key
            if self.current_record is not None
            else None
        )
        self.records = records
        self._records_by_key = {
            record.key: record for record in records
        }
        self.table.setRowCount(0)
        selected_row = -1
        pending_task_id = getattr(
            self,
            "_pending_show_task_id",
            None,
        )
        for record in records:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                record.kind_label,
                record.main_code,
                record.secondary_code,
                record.product_code,
                record.operator_name,
                MODEL_LABELS.get(record.model, record.model),
                record.status_label,
                record.created_at.strftime("%Y-%m-%d %H:%M"),
                str(record.result_count),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(HISTORY_KEY_ROLE, record.key)
                    item.setData(HISTORY_RECORD_ROLE, record)
                self.table.setItem(row, column, item)
            if record.key == current_key:
                selected_row = row
            if (
                pending_task_id is not None
                and record.kind == "ordinary"
                and record.task_id == pending_task_id
            ):
                selected_row = row
        self.pagination.set_result(
            total,
            self._page_request.page,
            self._page_request.page_size,
        )
        if records:
            self.table.selectRow(selected_row if selected_row >= 0 else 0)
            if pending_task_id is not None:
                self._pending_show_task_id = None
        else:
            self.current_record = None
            self.detail_title.setText("没有符合条件的历史记录")
            self.detail_meta.clear()
            self.prompt_view.clear()
            self.dual_preview.clear()
            self._set_actions(False)

    def _set_project_filter_names(self, names: list[str]) -> None:
        if not names:
            return
        selected = self.project_filter.currentData()
        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem("全部项目", None)
        for name in names:
            self.project_filter.addItem(name, name)
        index = self.project_filter.findData(selected)
        self.project_filter.setCurrentIndex(max(0, index))
        self.project_filter.blockSignals(False)

    def _filter_changed(self, *_args) -> None:
        self.project_filter.setEnabled(
            self.type_filter.currentData() != "ordinary"
        )
        self._page_request = PageRequest(
            page=1,
            page_size=100,
            sort_field=self._sort_field,
            sort_direction=self._sort_direction,
        )
        if not self.async_mode:
            self._apply_filters()
            return
        if self.sender() is self.product_search:
            self._keyword_timer.start()
        else:
            self._request_history_page()

    def _page_requested(self, page: int) -> None:
        self._page_request = PageRequest(
            page=page,
            page_size=100,
            sort_field=self._sort_field,
            sort_direction=self._sort_direction,
        )
        if self.async_mode:
            self._request_history_page()

    def _sort_requested(self, column: int) -> None:
        fields = {
            0: "kind",
            1: "main_code",
            2: "secondary_code",
            3: "product_code",
            4: "operator_name",
            5: "model",
            6: "status",
            7: "created_at",
            8: "result_count",
        }
        field = fields.get(column)
        if field is None:
            return
        if field == self._sort_field:
            self._sort_direction = (
                "asc" if self._sort_direction == "desc" else "desc"
            )
        else:
            self._sort_field = field
            self._sort_direction = "asc"
        self.table.horizontalHeader().setSortIndicator(
            column,
            Qt.SortOrder.AscendingOrder
            if self._sort_direction == "asc"
            else Qt.SortOrder.DescendingOrder,
        )
        self._page_request = PageRequest(
            page=1,
            page_size=100,
            sort_field=self._sort_field,
            sort_direction=self._sort_direction,
        )
        self._request_history_page()

    def _project_record(self, row: dict) -> HistoryRecord:
        created = utc_naive_to_local(row["created_at"])
        status = str(row["status"])
        if status == ATTEMPT_SUCCEEDED and int(
            row.get("manual_result_count") or 0
        ):
            status = "manual_ps"
        return HistoryRecord(
            kind="project",
            record_id=int(row["id"]),
            task_id=int(row["task_id"]),
            attempt_id=int(row["id"]),
            status=status,
            created_at=created,
            project_name=str(
                row.get("access_project_name")
                or row.get("project_name")
                or row.get("product_code")
                or ""
            ),
            product_code=str(row.get("product_code") or ""),
            main_code=str(row.get("project_code") or ""),
            secondary_code=str(row.get("task_code") or ""),
            operator_name=str(row.get("operator_name") or ""),
            model=str(row.get("model") or ""),
            provider_name=str(row.get("provider_name") or ""),
            prompt=str(row.get("prompt_text") or ""),
            result_count=int(row.get("result_count") or 0),
            raw=row,
        )

    def _ordinary_record(self, task: dict) -> HistoryRecord:
        created = datetime.fromisoformat(str(task["created_at"]))
        created = utc_naive_to_local(created)
        prompt = str(task.get("prompt") or "")
        title = prompt.strip().splitlines()[0][:80] if prompt else "未命名创作"
        return HistoryRecord(
            kind="ordinary",
            record_id=int(task["id"]),
            task_id=int(task["id"]),
            attempt_id=None,
            status=str(task["status"]),
            created_at=created,
            project_name="普通创作",
            product_code=title,
            main_code=f"任务 #{task['id']}",
            secondary_code=MODE_LABELS.get(
                task["mode"],
                task["mode"],
            ),
            operator_name="本机",
            model=str(task.get("model") or ""),
            provider_name="自动/历史",
            prompt=prompt,
            result_count=int(task.get("result_count") or 0),
            raw=task,
        )

    def _refresh_project_filter(self) -> None:
        selected = self.project_filter.currentData()
        names = sorted({
            record.project_name
            for record in self.records
            if record.kind == "project" and record.project_name
        })
        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem("全部项目", None)
        for name in names:
            self.project_filter.addItem(name, name)
        index = self.project_filter.findData(selected)
        self.project_filter.setCurrentIndex(max(0, index))
        self.project_filter.blockSignals(False)

    def _reset_filters(self) -> None:
        widgets = (
            self.project_filter,
            self.product_search,
            self.status_filter,
            self.type_filter,
            self.start_date,
            self.end_date,
        )
        for widget in widgets:
            widget.blockSignals(True)
        self.project_filter.setCurrentIndex(0)
        self.product_search.clear()
        self.status_filter.setCurrentIndex(0)
        self.type_filter.setCurrentIndex(0)
        self.start_date.clear()
        self.end_date.clear()
        for widget in widgets:
            widget.blockSignals(False)
        self._filter_changed()

    def _apply_filters(self, *_args) -> None:
        project_name = self.project_filter.currentData()
        product = self.product_search.text().strip().casefold()
        status = self.status_filter.currentData()
        kind = self.type_filter.currentData()
        start = self.start_date.python_date()
        end = self.end_date.python_date()
        rows = []
        for record in self.records:
            if kind and record.kind != kind:
                continue
            if (
                project_name
                and record.kind == "project"
                and record.project_name != project_name
            ):
                continue
            if product and product not in record.product_code.casefold():
                continue
            if status and record.status not in (
                {status, TASK_SUCCESS}
                if status == ATTEMPT_SUCCEEDED
                else {status}
            ):
                continue
            created = record.created_at.date()
            if start is not None and created < start:
                continue
            if end is not None and created > end:
                continue
            rows.append(record)
        self._records_by_key = {record.key: record for record in rows}
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for record in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                record.kind_label,
                record.main_code,
                record.secondary_code,
                record.product_code,
                record.operator_name,
                MODEL_LABELS.get(record.model, record.model),
                record.status_label,
                record.created_at.strftime("%Y-%m-%d %H:%M"),
                str(record.result_count),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(HISTORY_KEY_ROLE, record.key)
                    item.setData(HISTORY_RECORD_ROLE, record)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        if self.table.rowCount():
            self.table.selectRow(0)
        else:
            self.current_record = None
            self.detail_title.setText("没有符合条件的历史记录")
            self.detail_meta.clear()
            self.prompt_view.clear()
            self.dual_preview.clear()
            self._set_actions(False)

    def _selection_changed(self) -> None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        record = (
            item.data(HISTORY_RECORD_ROLE)
            if item is not None
            else None
        )
        if record is None:
            return
        self.current_record = record
        self._load_record(record)

    def _find_task_row(self, task_id: int) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            record = (
                item.data(HISTORY_RECORD_ROLE)
                if item is not None
                else None
            )
            if (
                record is not None
                and record.kind == "ordinary"
                and record.task_id == task_id
            ):
                return row
        return -1

    def show_task(self, task_id: int) -> None:
        if self.async_mode:
            self._pending_show_task_id = task_id
            widgets = (
                self.project_filter,
                self.product_search,
                self.status_filter,
                self.type_filter,
                self.start_date,
                self.end_date,
            )
            for widget in widgets:
                widget.blockSignals(True)
            self.project_filter.setCurrentIndex(0)
            self.product_search.setText(str(task_id))
            self.status_filter.setCurrentIndex(0)
            self.type_filter.setCurrentIndex(
                self.type_filter.findData("ordinary")
            )
            self.start_date.clear()
            self.end_date.clear()
            for widget in widgets:
                widget.blockSignals(False)
            self._page_request = PageRequest(page=1, page_size=100)
            self._request_history_page()
            return
        row = self._find_task_row(task_id)
        if row < 0:
            self.refresh()
            row = self._find_task_row(task_id)
        if row >= 0:
            self.table.selectRow(row)

    def _load_record(self, record: HistoryRecord) -> None:
        if self.async_mode:
            self._active_detail_request = self.query_runner.submit(
                lambda: self._load_record_data(record)
            )
            self.detail_title.setText("正在加载历史详情…")
            return
        assets, model_reference_path, meta = self._load_record_data(record)
        self._render_record_detail(
            record,
            assets,
            model_reference_path,
            meta,
        )

    def _load_record_data(self, record: HistoryRecord):
        assets = []
        model_reference_path: Path | None = None
        if record.kind == "project":
            task = self.repository.get_task(record.task_id)
            if task is None:
                return [], None, "任务不存在"
            references = [
                StoredImage(
                    path=self.resolver.resolve_project_path(
                        task.project_relative_root,
                        asset.relative_path,
                    ),
                    content_hash=asset.content_hash,
                    mime=asset.mime,
                    width=asset.width,
                    height=asset.height,
                    original_name=asset.original_name,
                )
                for asset in task.garment_assets
            ]
            model_reference_path = self.resolver.resolve_project_path(
                task.project_relative_root,
                task.model_path,
            )
            references.append(
                StoredImage(
                    path=model_reference_path,
                    content_hash=task.model_hash,
                    mime="image/png",
                    width=0,
                    height=0,
                    original_name=task.model_name,
                )
            )
            results = []
            for result in self.repository.list_attempt_results(
                int(record.attempt_id)
            ):
                path = self.resolver.resolve_project_path(
                    task.project_relative_root,
                    result.relative_path,
                )
                results.append(
                    {
                        "path": path,
                        "thumbnail_path": path,
                        "result": result,
                    }
                )
            assets = build_task_assets(references, results)
            provider_text = record.provider_name or "自动/历史"
            requested_provider = str(
                record.raw.get("requested_provider_name") or ""
            )
            if requested_provider and requested_provider != provider_text:
                provider_text += f"（首选 {requested_provider}）"
            meta = (
                f"{record.kind_label} · {record.project_name} · "
                f"{record.product_code}\n"
                f"项目 {record.main_code} · 子任务 {record.secondary_code}\n"
                f"模型 {MODEL_LABELS.get(record.model, record.model)} · "
                f"供应商 {provider_text} · "
                f"状态 {record.status_label}\n"
                f"宽高比 {record.raw.get('aspect_ratio') or '自动'} · "
                f"分辨率 {record.raw.get('resolution') or '自动'} · "
                f"数量 {record.raw.get('n') or 0}"
            )
        else:
            task_id = int(record.task_id)
            assets = build_task_assets(
                self.database.get_task_references(task_id),
                self.database.get_task_results(task_id),
            )
            if record.raw.get("mode") in (
                MODE_DRESS_UP,
                MODE_FREE_COMPOSE,
            ):
                references = [
                    asset
                    for asset in assets
                    if asset.is_reference
                ]
                if references:
                    model_reference_path = references[-1].path
            meta = (
                f"普通创作 · {record.secondary_code} · "
                f"{MODEL_LABELS.get(record.model, record.model)}\n"
                f"{record.main_code} · 状态 {record.status_label}\n"
                f"宽高比 {record.raw.get('aspect_ratio') or '自动'} · "
                f"分辨率 {record.raw.get('resolution') or '自动'} · "
                f"数量 {record.raw.get('n') or 0}"
            )
            if record.raw.get("auto_strategy"):
                meta += (
                    "\n自动策略："
                    + AUTO_STRATEGY_LABELS.get(
                        str(record.raw["auto_strategy"]),
                        str(record.raw["auto_strategy"]),
                    )
                )
            if record.raw.get("auto_reason"):
                meta += f"\n判断：{record.raw['auto_reason']}"
        elapsed = format_task_elapsed(record.raw)
        if elapsed != "—":
            meta += f"\n总耗时：{elapsed}"
        error_message = str(record.raw.get("error_message") or "")
        if error_message:
            meta += f"\n错误：{error_message}"
        return assets, model_reference_path, meta

    def _render_record_detail(
        self,
        record: HistoryRecord,
        assets: list,
        model_reference_path: Path | None,
        meta: str,
    ) -> None:
        self.current_assets = assets
        self.detail_title.setText(record.main_code)
        self.detail_meta.setText(meta)
        self.prompt_view.setPlainText(record.prompt)
        refs = [asset for asset in assets if asset.is_reference]
        results = [asset for asset in assets if asset.is_result]
        result_items = [
            (
                asset.thumbnail_path,
                asset.path,
                asset.label,
                asset,
            )
            for asset in results
        ]
        reference_items = [
            (
                asset.thumbnail_path,
                asset.path,
                (
                    "模特"
                    if model_reference_path is not None
                    and asset.path.resolve()
                    == model_reference_path.resolve()
                    else asset.label
                ),
                asset,
            )
            for asset in refs
        ]
        preferred_reference = next(
            (
                index
                for index, (_, _, label, _) in enumerate(
                    reference_items
                )
                if label == "模特"
            ),
            0,
        )
        self.dual_preview.set_assets(
            result_items,
            reference_items,
            default_reference_row=preferred_reference,
        )
        if results:
            self.dual_preview.result_pane.list_widget.setCurrentRow(0)
            self._preview_selected(
                "result",
                results[0],
                results[0].path,
            )
        elif refs:
            self.dual_preview.reference_pane.list_widget.setCurrentRow(
                preferred_reference
            )
        else:
            self.current_selected_asset = None
        self._update_actions(record)

    def _preview_selected(self, _role: str, payload, _path) -> None:
        self.current_selected_asset = payload
        can_save = bool(
            payload is not None and getattr(payload, "is_result", False)
        )
        self.save_local_button.setEnabled(can_save)
        self.save_library_button.setEnabled(can_save)

    def _update_actions(self, record: HistoryRecord) -> None:
        if record.kind == "ordinary":
            cancel = record.status in (TASK_QUEUED, TASK_RUNNING)
            retry = record.status in (
                TASK_SUCCESS,
                TASK_FAILED,
                TASK_CANCELLED,
            )
        else:
            owns_attempt = bool(
                self.actor.role == ROLE_ADMIN
                or int(record.raw.get("operator_id") or 0)
                == self.actor.id
            )
            cancel = (
                record.status == ATTEMPT_QUEUED
                and owns_attempt
            )
            retry = bool(
                record.status
                not in (ATTEMPT_QUEUED, ATTEMPT_RUNNING)
                and (
                    self.actor.role == ROLE_ADMIN
                    or int(record.raw.get("assignee_id") or 0)
                    == self.actor.id
                )
            )
        self.cancel_button.setEnabled(cancel)
        self.retry_button.setEnabled(retry)
        selected_result = bool(
            self.current_selected_asset is not None
            and getattr(self.current_selected_asset, "is_result", False)
        )
        self.save_local_button.setEnabled(selected_result)
        self.save_library_button.setEnabled(selected_result)

    def _set_actions(self, enabled: bool) -> None:
        for button in (
            self.cancel_button,
            self.retry_button,
            self.save_local_button,
            self.save_library_button,
        ):
            button.setEnabled(enabled)

    def _cancel_current(self) -> None:
        record = self.current_record
        if record is None:
            return
        if record.kind == "ordinary":
            if record.status == TASK_RUNNING:
                answer = QMessageBox.warning(
                    self,
                    "停止等待",
                    "停止等待不会保证服务端终止请求，仍可能产生费用。\n"
                    "是否继续？",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.worker.request_cancel(record.task_id)
        else:
            try:
                self.repository.cancel_queued_generation_attempt(
                    int(record.attempt_id),
                    self.actor,
                )
            except Exception as exc:
                QMessageBox.warning(self, "取消失败", str(exc))
                return
            self.project_attempt_cancelled.emit(int(record.attempt_id))
        self.refresh()

    def _retry_current(self) -> None:
        record = self.current_record
        if record is None:
            return
        if record.kind == "ordinary":
            self.retry_requested.emit(record.task_id)
            return
        self.project_retry_requested.emit(
            record.task_id,
            int(record.attempt_id),
        )

    def _save_local(self) -> None:
        record = self.current_record
        asset = self.current_selected_asset
        if record is None or asset is None or not asset.is_result:
            return
        if record.kind == "ordinary":
            from ..local_export import LocalImageExporter

            exporter = LocalImageExporter(
                self.database,
                self.app_settings,
            )
            result = asset.payload
            directory = exporter.preferred_directory(record.task_id)
            if directory is None:
                directory_text = QFileDialog.getExistingDirectory(
                    self,
                    "选择保存目录",
                    str(self.app_settings.last_folder() or Path.home()),
                )
                if not directory_text:
                    return
                directory = Path(directory_text)
            saved = exporter.save_result(
                record.task_id,
                int(result["id"]),
                directory,
            )
            show_saved_toast(self, saved)
            return
        directory = self._project_save_directory()
        if directory is None:
            return
        result = asset.payload["result"]
        saved = export_original_image(
            asset.path,
            directory,
            task_code=record.secondary_code,
            result_index=result.result_index,
        )
        show_saved_toast(self, saved)

    def _project_save_directory(self) -> Path | None:
        mode = self.app_settings.local_save_mode()
        if mode == LOCAL_SAVE_FIXED:
            folder = self.app_settings.fixed_folder()
            if folder and folder.is_dir():
                return folder
        if mode == LOCAL_SAVE_REFERENCE:
            reference = next(
                (
                    asset
                    for asset in self.current_assets
                    if asset.is_reference
                ),
                None,
            )
            if reference is not None:
                folder = Path(reference.path).parent.parent / "AI图"
                folder.mkdir(parents=True, exist_ok=True)
                return folder
        last = self.app_settings.last_folder()
        if last and last.is_dir():
            return last
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择保存目录",
            str(Path.home()),
        )
        if not directory:
            return None
        folder = Path(directory)
        self.app_settings.set_last_folder(folder)
        return folder

    def _save_library(self) -> None:
        record = self.current_record
        asset = self.current_selected_asset
        if record is None or asset is None or not asset.is_result:
            return
        try:
            if record.kind == "ordinary":
                result = asset.payload
                work_id = self.library_service.save_task_result(
                    record.task_id,
                    int(result["id"]),
                )
            else:
                result = asset.payload["result"]
                reference_snapshots = None
                try:
                    from ..collaboration.local_sources import (
                        LocalTaskSourceStore,
                    )

                    task = self.repository.get_task(record.task_id)
                    if task is not None:
                        reference_snapshots = (
                            LocalTaskSourceStore.read_attempt_snapshot(
                            self.library_service.file_store.paths.root
                            / "local_sources",
                            self.actor.id,
                            task,
                            int(record.attempt_id),
                            )
                        )
                except Exception:
                    reference_snapshots = None
                work_id = self.library_service.save_project_result(
                    repository=self.repository,
                    resolver=self.resolver,
                    task_id=record.task_id,
                    attempt_id=int(record.attempt_id),
                    result=result,
                    reference_snapshots=reference_snapshots,
                )
            folder = self.library_service.work_folder(work_id)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.library_changed.emit()
        show_saved_path(
            self,
            title="已保存到作品库",
            message="图片、参考图和生成信息已保存。",
            path=folder,
        )
