from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from .components import FilterBar, PageActionBar, PageHeader
from .date_filter import OptionalDateEdit
from .pagination_bar import PaginationBar
from .widgets import ImageStrip


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
        self._keyword_timer.timeout.connect(self._request_projects)
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
        header = QHBoxLayout()
        page_header = PageHeader(
            "待分配",
            "选择完整未分配的主任务并整组领取；任一子任务已被领取时整批取消。"
        )
        self.claim_button = QPushButton("领取所选")
        self.claim_button.setObjectName("PrimaryButton")
        self.claim_button.setEnabled(False)
        self.claim_button.clicked.connect(self._claim_selected)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        page_header.add_action(refresh)
        header.addWidget(page_header)
        outer.addLayout(header)

        filter_bar = FilterBar()
        filters = filter_bar.layout
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索任务编码、货号、项目或备注")
        self.start_date_edit = OptionalDateEdit("开始日期")
        self.end_date_edit = OptionalDateEdit("结束日期")
        reset_filter = QPushButton("重置筛选")
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(QLabel("创建时间"))
        filters.addWidget(self.start_date_edit)
        filters.addWidget(QLabel("至"))
        filters.addWidget(self.end_date_edit)
        filters.addWidget(reset_filter)
        outer.addWidget(filter_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
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
        self.project_table = QTableWidget(0, 6)
        self.project_table.setHorizontalHeaderLabels(
            [
                "主任务编码",
                "货号",
                "项目",
                "子任务数",
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
        for column, width in enumerate((200, 90, 150, 80, 60, 135)):
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
        task_host = QWidget()
        task_layout = QVBoxLayout(task_host)
        task_layout.setContentsMargins(8, 0, 0, 0)
        task_title = QLabel("子任务明细")
        task_title.setStyleSheet("font-size:16px;font-weight:700;")
        task_layout.addWidget(task_title)
        self.task_table = QTableWidget(0, 5)
        self.task_table.setHorizontalHeaderLabels(
            [
                "子任务编码",
                "衣服数",
                "状态",
                "制作人员",
                "创建时间",
            ]
        )
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.task_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.task_table.setSortingEnabled(True)
        self.task_table.horizontalHeader().setStretchLastSection(True)
        self.task_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((210, 70, 90, 110, 135)):
            self.task_table.setColumnWidth(column, width)
        self.task_table.itemSelectionChanged.connect(self._task_selected)
        task_layout.addWidget(self.task_table, 1)
        self.task_pagination = PaginationBar()
        self.task_pagination.page_requested.connect(
            self._task_page_requested
        )
        task_layout.addWidget(self.task_pagination)
        right.addWidget(task_host)

        source_host = QWidget()
        source_layout = QVBoxLayout(source_host)
        source_layout.setContentsMargins(8, 8, 0, 0)
        source_title = QLabel("参考图")
        source_title.setStyleSheet("font-size:16px;font-weight:700;")
        source_layout.addWidget(source_title)
        self.source_strip = ImageStrip(icon_size=126)
        source_layout.addWidget(self.source_strip)
        source_layout.addStretch(1)
        right.addWidget(source_host)
        right.setSizes([430, 220])
        splitter.addWidget(right)
        splitter.setSizes([540, 660])
        outer.addWidget(splitter, 1)
        action_bar = PageActionBar()
        action_bar.layout.addStretch(1)
        action_bar.layout.addWidget(self.claim_button)
        outer.addWidget(action_bar)
        self.search_edit.textChanged.connect(self._filter_changed)
        self.start_date_edit.dateChanged.connect(self._filter_changed)
        self.end_date_edit.dateChanged.connect(self._filter_changed)
        reset_filter.clicked.connect(self._reset_filters)
        if self.async_mode:
            self.project_table.setSortingEnabled(False)
            self.task_table.setSortingEnabled(False)
            self.project_table.horizontalHeader().setSortIndicatorShown(True)
            self.task_table.horizontalHeader().setSortIndicatorShown(True)
            self.project_table.horizontalHeader().setSortIndicator(
                5,
                Qt.SortOrder.DescendingOrder,
            )
            self.task_table.horizontalHeader().setSortIndicator(
                4,
                Qt.SortOrder.DescendingOrder,
            )
            self.project_table.horizontalHeader().sectionClicked.connect(
                self._project_sort_requested
            )
            self.task_table.horizontalHeader().sectionClicked.connect(
                self._task_sort_requested
            )

    def refresh(self) -> None:
        if self.async_mode:
            self._request_projects()
            return
        try:
            shared_root = self.repository.get_shared_setting("share_root")
            if shared_root:
                self.resolver = SharedPathResolver(shared_root)
            self.projects = self.repository.list_assignment_projects(
                self.actor
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
                str(project.task_count),
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
            self.tasks = []
            self.task_table.setRowCount(0)
            self.source_strip.clear()
        self._selection_changed()

    def _task_query(self) -> TaskQuery:
        return TaskQuery(
            keyword=self.search_edit.text().strip(),
            start_date=self.start_date_edit.python_date(),
            end_date=self.end_date_edit.python_date(),
        )

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
        self.task_pagination.set_result(0, 1, 100)
        self.source_strip.clear()
        page = PageRequest(
            page=self._task_page.page,
            page_size=100,
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
        self._selection_changed()
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
                str(project.task_count),
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
        if self.projects and not selected_ids:
            self.project_table.selectRow(0)
        if not self.projects:
            self.tasks = []
            self._tasks_by_id = {}
            self.task_table.setRowCount(0)
            self.task_pagination.set_result(0, 1, 100)
            self.source_strip.clear()
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
        self.task_pagination.set_result(
            result.total,
            result.page,
            result.page_size,
        )
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
            self.task_table.selectRow(target_row)
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
        if not self.async_mode:
            return
        if self.sender() is self.search_edit:
            self._keyword_timer.start()
        else:
            self._request_projects()

    def _reset_filters(self) -> None:
        for widget in (
            self.search_edit,
            self.start_date_edit,
            self.end_date_edit,
        ):
            widget.blockSignals(True)
        self.search_edit.clear()
        self.start_date_edit.clear()
        self.end_date_edit.clear()
        for widget in (
            self.search_edit,
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

    def _task_page_requested(self, page: int) -> None:
        project = self._current_project()
        if project is None:
            return
        self._task_page = PageRequest(
            page=page,
            page_size=100,
            sort_field=self._task_sort_field,
            sort_direction=self._task_sort_direction,
        )
        if self.async_mode:
            self._request_tasks(project.id)

    def _project_sort_requested(self, column: int) -> None:
        fields = {
            0: "project_code",
            1: "product_code",
            2: "project_name",
            3: "task_count",
            4: "is_urgent",
            5: "created_at",
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
        project_id = project.id if project is not None else None
        if project_id is not None:
            if self.async_mode:
                self._task_page = PageRequest(
                    page=1,
                    page_size=100,
                    sort_field=self._task_sort_field,
                    sort_direction=self._task_sort_direction,
                )
                self._request_tasks(
                    project_id,
                    preserve_selection=False,
                )
            else:
                self._load_project_tasks(project_id)

    def _load_project_tasks(self, project_id: int) -> None:
        try:
            self.tasks = self.repository.list_project_tasks(project_id)
        except Exception as exc:
            QMessageBox.warning(self, "读取子任务失败", str(exc))
            return
        self._tasks_by_id = {task.id: task for task in self.tasks}
        self.task_table.setSortingEnabled(False)
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
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        task.id,
                    )
                self.task_table.setItem(row, column, item)
        self.task_table.setSortingEnabled(True)
        if self.task_table.rowCount():
            self.task_table.selectRow(0)
        else:
            self.source_strip.clear()

    def _task_selected(self) -> None:
        row = self.task_table.currentRow()
        item = self.task_table.item(row, 0) if row >= 0 else None
        task_id = (
            int(item.data(Qt.ItemDataRole.UserRole))
            if item is not None
            else None
        )
        task = self._tasks_by_id.get(task_id)
        if task is None:
            self.source_strip.clear()
            return
        images: list[tuple[object, str, object]] = []
        for index, asset in enumerate(task.garment_assets, start=1):
            path = self.resolver.resolve_project_path(
                task.project_relative_root,
                asset.relative_path,
            )
            images.append((path, f"衣服 {index}", asset.relative_path))
        model_path = self.resolver.resolve_project_path(
            task.project_relative_root,
            task.model_path,
        )
        images.append((model_path, "模特", task.model_path))
        if self.async_mode:
            self.source_strip.set_images_async(images)
        else:
            self.source_strip.set_images(images)

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
