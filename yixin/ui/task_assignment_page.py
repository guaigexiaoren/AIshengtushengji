from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    TASK_APPROVED,
    TASK_AVAILABLE,
    TASK_AWAITING_ACCEPTANCE,
    TASK_CANCELLED,
    TASK_CLAIMED,
    TASK_EXPANDED,
    TASK_RETURNED,
    TASK_SKIPPED,
    TASK_WORKING,
    TASK_STATUS_LABELS,
    AuthUser,
    ProjectRecord,
    ProjectTask,
)
from ..time_utils import utc_naive_to_local
from ..paging import PageRequest, PageResult, TaskQuery
from .async_queries import get_query_runner
from .components import FilterBar, PageActionBar
from .date_filter import OptionalDateEdit
from .image_preview import ImagePreviewPane
from .pagination_bar import PaginationBar


class TaskAssignmentPage(QWidget):
    projects_claimed = Signal(int)

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
            get_query_runner() if self.async_mode else None
        )
        self._project_page = PageRequest()
        self._task_page = PageRequest()
        self._project_sort_field = "created_at"
        self._project_sort_direction = "desc"
        self._task_sort_field = "created_at"
        self._task_sort_direction = "desc"
        self._active_project_request = -1
        self._active_task_request = -1
        self._keyword_timer = QTimer(self)
        self._keyword_timer.setSingleShot(True)
        self._keyword_timer.setInterval(350)
        self._keyword_timer.timeout.connect(self._keyword_timeout)
        self.projects: list[ProjectRecord] = []
        self.tasks: list[ProjectTask] = []
        self._projects_by_id: dict[int, ProjectRecord] = {}
        self._tasks_by_id: dict[int, ProjectTask] = {}
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
        self.claim_button = QPushButton("领取所选")
        self.claim_button.setObjectName("PrimaryButton")
        self.claim_button.setEnabled(False)
        self.claim_button.clicked.connect(self._claim_selected)
        refresh = QPushButton("刷新")
        refresh.setObjectName("SecondaryButton")
        refresh.clicked.connect(self.refresh)

        filter_bar = FilterBar()
        filters = filter_bar.layout
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索主任务编码、货号、项目或备注")
        self.project_filter = QComboBox()
        self.project_filter.setMinimumWidth(180)
        self.urgent_filter = QComboBox()
        self.urgent_filter.addItem("全部加急情况", None)
        self.urgent_filter.addItem("仅加急", True)
        self.urgent_filter.addItem("仅非加急", False)
        self.start_date_edit = OptionalDateEdit("开始日期")
        self.end_date_edit = OptionalDateEdit("结束日期")
        reset_filter = QPushButton("重置筛选")
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(QLabel("项目"))
        filters.addWidget(self.project_filter)
        filters.addWidget(QLabel("加急"))
        filters.addWidget(self.urgent_filter)
        filters.addWidget(QLabel("创建时间"))
        filters.addWidget(self.start_date_edit)
        filters.addWidget(QLabel("至"))
        filters.addWidget(self.end_date_edit)
        filters.addWidget(reset_filter)
        filters.addWidget(refresh)
        outer.addWidget(filter_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QFrame()
        left.setObjectName("Card")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        project_header = QHBoxLayout()
        project_title = QLabel("主任务")
        project_title.setStyleSheet("font-size:16px;font-weight:700;")
        selection_hint = QLabel(
            "按住 Ctrl 多选，按住 Shift 连续选择"
        )
        selection_hint.setStyleSheet("color:#7a879c;font-size:10px;")
        project_header.addWidget(project_title)
        project_header.addStretch(1)
        project_header.addWidget(selection_hint)
        left_layout.addLayout(project_header)
        self.project_table = QTableWidget(0, 7)
        self.project_table.setHorizontalHeaderLabels(
            [
                "主任务编码",
                "货号",
                "项目",
                "衣服数",
                "模特数",
                "加急",
                "创建时间",
            ]
        )
        self.project_table.verticalHeader().setVisible(False)
        self.project_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.project_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.project_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.project_table.setSortingEnabled(True)
        self.project_table.horizontalHeader().setStretchLastSection(True)
        self.project_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((200, 100, 140, 70, 70, 60, 135)):
            self.project_table.setColumnWidth(column, width)
        self.project_table.itemSelectionChanged.connect(
            self._selection_changed
        )
        left_layout.addWidget(self.project_table, 1)
        self.project_pagination = PaginationBar()
        self.project_pagination.page_requested.connect(
            self._project_page_requested
        )
        left_layout.addWidget(self.project_pagination)
        splitter.addWidget(left)

        right = QSplitter(Qt.Orientation.Vertical)
        self.garment_preview = ImagePreviewPane(
            "garment_source",
            "服装素材",
            async_thumbnails=self.async_mode,
        )
        self.model_preview = ImagePreviewPane(
            "model_source",
            "模特素材",
            async_thumbnails=self.async_mode,
        )
        right.addWidget(self.garment_preview)
        right.addWidget(self.model_preview)
        right.setSizes([420, 300])
        splitter.addWidget(right)
        splitter.setSizes([900, 600])
        outer.addWidget(splitter, 1)
        action_bar = PageActionBar()
        action_bar.layout.addStretch(1)
        action_bar.layout.addWidget(self.claim_button)
        outer.addWidget(action_bar)
        self.search_edit.textChanged.connect(self._filter_changed)
        self.project_filter.currentIndexChanged.connect(
            self._filter_changed
        )
        self.urgent_filter.currentIndexChanged.connect(
            self._filter_changed
        )
        self.start_date_edit.dateChanged.connect(self._filter_changed)
        self.end_date_edit.dateChanged.connect(self._filter_changed)
        reset_filter.clicked.connect(self._reset_filters)
        if self.async_mode:
            self.project_table.setSortingEnabled(False)
            header = self.project_table.horizontalHeader()
            header.setSortIndicatorShown(True)
            header.setSortIndicator(
                6,
                Qt.SortOrder.DescendingOrder,
            )
            header.sectionClicked.connect(
                self._project_sort_requested
            )

    def refresh(self) -> None:
        try:
            shared_root = self.repository.get_shared_setting("share_root")
        except Exception:
            shared_root = None
        if shared_root:
            self.resolver = SharedPathResolver(shared_root)
        try:
            self._refresh_project_filter()
        except Exception as exc:
            QMessageBox.warning(self, "读取项目筛选失败", str(exc))
            return
        if self.async_mode:
            self._request_projects()
            return
        try:
            self.projects = self.repository.list_assignment_projects(
                self.actor,
                project_name=self.project_filter.currentData(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取待分配任务失败", str(exc))
            return
        self._projects_by_id = {
            project.id: project for project in self.projects
        }
        self.project_table.setSortingEnabled(False)
        self.project_table.setRowCount(0)
        for project in self.projects:
            row = self.project_table.rowCount()
            self.project_table.insertRow(row)
            values = (
                project.project_code,
                project.product_code or project.name,
                project.project_name,
                str(project.garment_count),
                str(project.model_count),
                "是" if project.is_urgent else "否",
                utc_naive_to_local(project.created_at).strftime(
                    "%Y-%m-%d %H:%M"
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        project.id,
                    )
                self.project_table.setItem(row, column, item)
        self.project_table.setSortingEnabled(True)
        if self.project_table.rowCount():
            self.project_table.selectRow(0)
        else:
            self.garment_preview.clear()
            self.model_preview.clear()
        self._selection_changed()

    def _task_query(self) -> TaskQuery:
        return TaskQuery(
            keyword=self.search_edit.text().strip(),
            project_name=self.project_filter.currentData(),
            start_date=self.start_date_edit.python_date(),
            end_date=self.end_date_edit.python_date(),
            is_urgent=self.urgent_filter.currentData(),
        )

    def _refresh_project_filter(self) -> None:
        current = self.project_filter.currentData()
        counts = self.repository.list_assignment_project_counts(
            self.actor
        )
        total = sum(count for _name, count in counts)
        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem(f"全部项目（{total}）", None)
        for name, count in counts:
            self.project_filter.addItem(f"{name}（{count}）", name)
        index = self.project_filter.findData(current)
        self.project_filter.setCurrentIndex(max(0, index))
        self.project_filter.blockSignals(False)

    def _request_projects(self) -> None:
        self._active_task_request = -1
        query = self._task_query()
        page = PageRequest(
            page=self._project_page.page,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        self.claim_button.setEnabled(False)
        self.claim_button.setText("正在加载…")
        self._active_project_request = self.query_runner.submit(
            lambda: self.repository.list_assignment_projects_page(
                self.actor,
                query,
                page,
            )
        )

    def _request_tasks(
        self,
        project_id: int,
        *,
        preserve_selection: bool = True,
    ) -> None:
        current_item = self.task_table.item(
            self.task_table.currentRow(),
            0,
        )
        self._pending_task_selection = (
            int(current_item.data(Qt.ItemDataRole.UserRole))
            if preserve_selection and current_item is not None
            else None
        )
        self.tasks = []
        self._tasks_by_id = {}
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(0)
        self.task_table.blockSignals(False)
        self.source_strip.clear()
        page = PageRequest(
            page=1,
            page_size=10000,
            sort_field=self._task_sort_field,
            sort_direction=self._task_sort_direction,
        )
        statuses = (
            TASK_AVAILABLE,
            TASK_CLAIMED,
            TASK_WORKING,
            TASK_AWAITING_ACCEPTANCE,
            TASK_RETURNED,
            TASK_APPROVED,
            TASK_SKIPPED,
            TASK_CANCELLED,
            TASK_EXPANDED,
        )
        self._active_task_request = self.query_runner.submit(
            lambda: self.repository.list_board_tasks_for_project_page(
                project_id,
                self.actor,
                statuses,
                TaskQuery(),
                page,
            )
        )

    def _query_succeeded(self, request_id: int, result) -> None:
        if not self.async_mode:
            return
        if request_id == self._active_project_request:
            self._render_projects(result)
        elif request_id == self._active_task_request:
            self._render_tasks(result)

    def _query_failed(self, request_id: int, message: str) -> None:
        if not self.async_mode or request_id not in (
            self._active_project_request,
            self._active_task_request,
        ):
            return
        # 失败时只恢复按钮状态并提示，不再自动重发查询，避免弹窗套娃。
        selected_ids = self._selected_project_ids()
        self.claim_button.setText(
            f"领取所选（{len(selected_ids)}）"
            if selected_ids
            else "领取所选"
        )
        self.claim_button.setEnabled(bool(selected_ids))
        QMessageBox.warning(self, "读取待分配任务失败", message)

    def _render_projects(
        self,
        result: PageResult[ProjectRecord],
    ) -> None:
        if result.total and result.page > result.total_pages:
            self._project_page = PageRequest(
                page=result.total_pages,
                page_size=100,
                sort_field=self._project_sort_field,
                sort_direction=self._project_sort_direction,
            )
            self._request_projects()
            return
        selected_ids = set(self._selected_project_ids())
        self.projects = list(result.items)
        self._projects_by_id = {
            project.id: project for project in self.projects
        }
        self._project_page = PageRequest(
            page=result.page,
            page_size=result.page_size,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        self.project_table.blockSignals(True)
        self.project_table.setRowCount(0)
        for project in self.projects:
            row = self.project_table.rowCount()
            self.project_table.insertRow(row)
            values = (
                project.project_code,
                project.product_code or project.name,
                project.project_name,
                str(project.garment_count),
                str(project.model_count),
                "是" if project.is_urgent else "否",
                utc_naive_to_local(project.created_at).strftime(
                    "%Y-%m-%d %H:%M"
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, project.id)
                self.project_table.setItem(row, column, item)
            if project.id in selected_ids:
                self.project_table.selectRow(row)
        self.project_table.blockSignals(False)
        self.project_pagination.set_result(
            result.total,
            result.page,
            result.page_size,
        )
        if self.projects and (
            not selected_ids or self.project_table.currentRow() < 0
        ):
            self.project_table.blockSignals(True)
            self.project_table.selectRow(0)
            self.project_table.blockSignals(False)
        if not self.projects:
            self.garment_preview.clear()
            self.model_preview.clear()
        self._selection_changed()

    def _render_tasks(
        self,
        result: PageResult[ProjectTask],
    ) -> None:
        if result.total and result.page > result.total_pages:
            project = self._current_project()
            if project is not None:
                self._task_page = PageRequest(
                    page=result.total_pages,
                    page_size=100,
                    sort_field=self._task_sort_field,
                    sort_direction=self._task_sort_direction,
                )
                self._request_tasks(project.id)
            return
        self.tasks = list(result.items)
        self._tasks_by_id = {task.id: task for task in self.tasks}
        self._task_page = PageRequest(
            page=result.page,
            page_size=result.page_size,
            sort_field=self._task_sort_field,
            sort_direction=self._task_sort_direction,
        )
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(0)
        for task in self.tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            values = (
                task.task_code,
                str(len(task.garment_assets)),
                TASK_STATUS_LABELS.get(task.status, task.status),
                task.assignee_name or "未分配",
                utc_naive_to_local(task.created_at).strftime(
                    "%Y-%m-%d %H:%M"
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, task.id)
                self.task_table.setItem(row, column, item)
        self.task_table.blockSignals(False)
        if self.tasks:
            target_row = next(
                (
                    row
                    for row, task in enumerate(self.tasks)
                    if task.id == getattr(
                        self,
                        "_pending_task_selection",
                        None,
                    )
                ),
                0,
            )
            self.task_table.blockSignals(True)
            self.task_table.selectRow(target_row)
            self.task_table.blockSignals(False)
            self._task_selected()
        else:
            self.source_strip.clear()

    def _filter_changed(self, *_args) -> None:
        self._project_page = PageRequest(
            page=1,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        if self.sender() is self.search_edit:
            self._keyword_timer.start()
        elif not self.async_mode:
            self.refresh()
        else:
            self._request_projects()

    def _keyword_timeout(self) -> None:
        if self.async_mode:
            self._request_projects()
        else:
            self.refresh()

    def _reset_filters(self) -> None:
        for widget in (
            self.search_edit,
            self.project_filter,
            self.urgent_filter,
            self.start_date_edit,
            self.end_date_edit,
        ):
            widget.blockSignals(True)
        self.search_edit.clear()
        self.project_filter.setCurrentIndex(0)
        self.urgent_filter.setCurrentIndex(0)
        self.start_date_edit.clear()
        self.end_date_edit.clear()
        for widget in (
            self.search_edit,
            self.project_filter,
            self.urgent_filter,
            self.start_date_edit,
            self.end_date_edit,
        ):
            widget.blockSignals(False)
        self._filter_changed()

    def _project_page_requested(self, page: int) -> None:
        self._project_page = PageRequest(
            page=page,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        if self.async_mode:
            self._request_projects()

    def _project_sort_requested(self, column: int) -> None:
        fields = {
            0: "project_code",
            1: "product_code",
            2: "project_name",
            3: "garment_count",
            4: "model_count",
            5: "is_urgent",
            6: "created_at",
        }
        field = fields.get(column)
        if field is None:
            return
        if field == self._project_sort_field:
            self._project_sort_direction = (
                "asc"
                if self._project_sort_direction == "desc"
                else "desc"
            )
        else:
            self._project_sort_field = field
            self._project_sort_direction = "asc"
        self.project_table.horizontalHeader().setSortIndicator(
            column,
            Qt.SortOrder.AscendingOrder
            if self._project_sort_direction == "asc"
            else Qt.SortOrder.DescendingOrder,
        )
        self._project_page = PageRequest(
            page=1,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        self._request_projects()

    def _task_sort_requested(self, column: int) -> None:
        fields = {
            0: "task_code",
            1: "garment_count",
            2: "status",
            3: "assignee_name",
            4: "created_at",
        }
        field = fields.get(column)
        if field is None:
            return
        if field == self._task_sort_field:
            self._task_sort_direction = (
                "asc"
                if self._task_sort_direction == "desc"
                else "desc"
            )
        else:
            self._task_sort_field = field
            self._task_sort_direction = "asc"
        self.task_table.horizontalHeader().setSortIndicator(
            column,
            Qt.SortOrder.AscendingOrder
            if self._task_sort_direction == "asc"
            else Qt.SortOrder.DescendingOrder,
        )
        project = self._current_project()
        if project is not None:
            self._task_page = PageRequest(
                page=1,
                page_size=100,
                sort_field=self._task_sort_field,
                sort_direction=self._task_sort_direction,
            )
            self._request_tasks(project.id)

    def _selected_project_ids(self) -> list[int]:
        selection = self.project_table.selectionModel()
        if selection is None:
            return []
        ids: list[int] = []
        for index in selection.selectedRows(0):
            item = self.project_table.item(index.row(), 0)
            if item is not None:
                ids.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return list(dict.fromkeys(ids))

    def _current_project(self) -> ProjectRecord | None:
        row = self.project_table.currentRow()
        item = self.project_table.item(row, 0) if row >= 0 else None
        if item is None:
            return None
        return self._projects_by_id.get(
            int(item.data(Qt.ItemDataRole.UserRole))
        )

    def _selection_changed(self) -> None:
        selected_ids = self._selected_project_ids()
        self.claim_button.setText(
            f"领取所选（{len(selected_ids)}）"
            if selected_ids
            else "领取所选"
        )
        self.claim_button.setEnabled(bool(selected_ids))
        project = self._current_project()
        if project is None:
            self.garment_preview.clear()
            self.model_preview.clear()
            return
        self._load_project_assets(project)

    def _load_project_assets(self, project: ProjectRecord) -> None:
        try:
            assets = self.repository.list_project_assets(project.id)
        except Exception as exc:
            QMessageBox.warning(self, "读取主任务素材失败", str(exc))
            return
        garment_items = []
        model_items = []
        for asset in assets:
            path = self.resolver.resolve_project_path(
                project.project_relative_root,
                asset.relative_path,
            )
            if asset.role == "garment":
                garment_items.append(
                    (
                        path,
                        f"服装 {len(garment_items) + 1}",
                        asset.relative_path,
                    )
                )
            elif asset.role == "model":
                model_items.append(
                    (
                        path,
                        f"模特 {len(model_items) + 1}",
                        asset.relative_path,
                    )
                )
        self.garment_preview.set_images(garment_items)
        self.model_preview.set_images(model_items)

    def _claim_selected(self) -> None:
        project_ids = self._selected_project_ids()
        if not project_ids:
            return
        try:
            claimed = self.repository.claim_projects(
                project_ids,
                self.actor,
            )
        except Exception as exc:
            QMessageBox.warning(self, "领取失败", str(exc))
            self.refresh()
            return
        QMessageBox.information(
            self,
            "领取完成",
            f"已领取 {len(claimed)} 个主任务，可进入“AI生图”继续制作。",
        )
        self.projects_claimed.emit(len(claimed))
        self.refresh()
