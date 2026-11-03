from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
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

from ..collaboration.repository import MySQLCentralRepository, utc_now
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    PROJECT_STATUS_LABELS,
    TASK_APPROVED,
    TASK_AVAILABLE,
    TASK_AWAITING_ACCEPTANCE,
    TASK_CANCELLED,
    TASK_CLAIMED,
    TASK_EXPANDED,
    TASK_RETURNED,
    TASK_SKIPPED,
    TASK_STATUS_LABELS,
    TASK_WORKING,
    AuthUser,
    ProjectRecord,
    ProjectTask,
)
from ..time_utils import utc_naive_to_local
from ..paging import PageRequest, PageResult, TaskQuery
from .async_queries import get_query_runner
from .components import FilterBar, PageHeader
from .date_filter import OptionalDateEdit
from .image_preview import DualImagePreview
from .pagination_bar import PaginationBar


RECORD_STATUSES = (
    TASK_AVAILABLE,
    TASK_CLAIMED,
    TASK_WORKING,
    TASK_AWAITING_ACCEPTANCE,
    TASK_RETURNED,
    TASK_APPROVED,
    TASK_SKIPPED,
    TASK_CANCELLED,
)
VIEW_TITLES = {
    "records": (
        "提交记录",
        "只读查看自己提交的任务、制作进度、参考图和成品。",
    ),
}


class SubmissionListPage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        view_mode: str,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
        async_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        if view_mode != "records":
            raise ValueError("提交记录页面只支持只读模式")
        self.view_mode = "records"
        self.async_mode = async_mode
        self.query_runner = (
            get_query_runner() if self.async_mode else None
        )
        self._active_result_request = -1
        self._active_result_task_id: int | None = None
        self._project_page = PageRequest()
        self._task_page = PageRequest()
        self._project_total = 0
        self._task_total = 0
        self._project_sort_field = "created_at"
        self._project_sort_direction = "desc"
        self._task_sort_field = "created_at"
        self._task_sort_direction = "desc"
        self._keyword_timer = QTimer(self)
        self._keyword_timer.setSingleShot(True)
        self._keyword_timer.setInterval(350)
        self._keyword_timer.timeout.connect(self._async_filter_requested)
        self.projects: list[ProjectRecord] = []
        self._projects_all: list[ProjectRecord] = []
        self.tasks: list[ProjectTask] = []
        self._all_tasks: list[ProjectTask] = []
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
        title_text, subtitle_text = VIEW_TITLES[self.view_mode]
        header = QHBoxLayout()
        page_header = PageHeader(title_text, subtitle_text)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        page_header.add_action(refresh)
        header.addWidget(page_header)
        outer.addLayout(header)

        filter_bar = FilterBar()
        filters = filter_bar.layout
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "搜索编码、货号、运营、制作人员或备注"
        )
        self.start_date_edit = self._optional_date_edit("开始日期")
        self.end_date_edit = self._optional_date_edit("结束日期")
        reset_filter = QPushButton("重置筛选")
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(QLabel("创建时间"))
        filters.addWidget(self.start_date_edit)
        filters.addWidget(QLabel("至"))
        filters.addWidget(self.end_date_edit)
        filters.addWidget(reset_filter)
        outer.addWidget(filter_bar)

        self.project_table = QTableWidget(0, 7)
        self.project_table.setHorizontalHeaderLabels(
            ["任务编码", "货号", "运营", "日期", "任务状态", "子任务进度", "加急"]
        )
        self.project_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.project_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.project_table.horizontalHeader().setStretchLastSection(True)
        self.project_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((210, 110, 130, 90, 110, 110, 70)):
            self.project_table.setColumnWidth(column, width)
        self.project_table.setMaximumHeight(220)
        self.project_table.setSortingEnabled(True)
        self.project_table.setColumnHidden(6, True)
        self.project_table.itemSelectionChanged.connect(self._project_selected)
        outer.addWidget(self.project_table)
        self.project_pagination = PaginationBar()
        self.project_pagination.page_requested.connect(
            self._project_page_requested
        )
        outer.addWidget(self.project_pagination)

        body = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 8, 8, 0)
        self.task_table = QTableWidget(0, 7)
        self.task_table.setHorizontalHeaderLabels(
            [
                "子任务编码",
                "衣服数",
                "制作人员",
                "状态",
                "加急",
                "创建时间",
                "更新时间",
            ]
        )
        self.task_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.task_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.task_table.horizontalHeader().setStretchLastSection(True)
        self.task_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((250, 70, 130, 100, 70, 145, 145)):
            self.task_table.setColumnWidth(column, width)
        self.task_table.setSortingEnabled(True)
        self.task_table.itemSelectionChanged.connect(self._task_selected)
        left_layout.addWidget(self.task_table, 1)
        self.task_pagination = PaginationBar()
        self.task_pagination.page_requested.connect(
            self._task_page_requested
        )
        left_layout.addWidget(self.task_pagination)
        self.info_line = QLabel("选择一个子任务查看详情")
        self.info_line.setWordWrap(False)
        self.info_line.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.info_line.setStyleSheet("color:#60708a;padding:4px 0;")
        left_layout.addWidget(self.info_line)
        body.addWidget(left)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)

        self.dual_preview = DualImagePreview(
            self.resolver,
            async_thumbnails=self.async_mode,
        )
        self.preview_splitter = self.dual_preview.splitter
        self.result_panel = self.dual_preview.result_pane
        self.reference_panel = self.dual_preview.reference_pane
        self.result_gallery = self.result_panel
        self.reference_gallery = self.reference_panel
        self.result_header = self.result_gallery.title_label
        self.reference_header = self.reference_gallery.title_label
        self.reference_strip = self.reference_gallery.list_widget
        self.reference_preview = self.reference_gallery.preview
        preview_layout.addWidget(self.dual_preview, 1)
        body.addWidget(preview_panel)
        body.setSizes([700, 620])
        outer.addWidget(body, 1)
        self.search_edit.textChanged.connect(self._filter_changed)
        self.start_date_edit.dateChanged.connect(self._filter_changed)
        self.end_date_edit.dateChanged.connect(self._filter_changed)
        reset_filter.clicked.connect(self._reset_filters)
        if self.async_mode:
            self.project_table.setSortingEnabled(False)
            self.task_table.setSortingEnabled(False)
            project_header = self.project_table.horizontalHeader()
            task_header = self.task_table.horizontalHeader()
            project_header.setSortIndicatorShown(True)
            task_header.setSortIndicatorShown(True)
            project_header.setSortIndicator(
                5,
                Qt.SortOrder.DescendingOrder,
            )
            task_header.setSortIndicator(
                5,
                Qt.SortOrder.DescendingOrder,
            )
            project_header.sectionClicked.connect(
                self._project_sort_requested
            )
            task_header.sectionClicked.connect(
                self._task_sort_requested
            )

    @staticmethod
    def _optional_date_edit(placeholder: str) -> OptionalDateEdit:
        return OptionalDateEdit(placeholder)

    def refresh(self) -> None:
        if self.async_mode:
            self._request_project_page()
            return
        try:
            shared_root = self.repository.get_shared_setting("share_root")
            if shared_root:
                self.resolver = SharedPathResolver(shared_root)
                self.dual_preview.set_resolver(self.resolver)
            self._all_tasks = self.repository.list_board_tasks(
                self.actor,
                include_finished=True,
                limit=10000,
            )
            projects = self.repository.list_projects(
                limit=10000,
                actor=self.actor,
            )
            self._all_tasks = [
                task
                for task in self._all_tasks
                if task.status != TASK_EXPANDED
            ]
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            return
        statuses = set(RECORD_STATUSES)
        visible_ids = {
            task.project_id
            for task in self._all_tasks
            if task.status in statuses
        }
        self._projects_all = [
            project for project in projects if project.id in visible_ids
        ]
        self._apply_filters()

    def _task_query(self) -> TaskQuery:
        return TaskQuery(
            keyword=self.search_edit.text().strip(),
            statuses=RECORD_STATUSES,
            start_date=self.start_date_edit.python_date(),
            end_date=self.end_date_edit.python_date(),
        )

    def _request_project_page(self) -> None:
        self._active_task_request = -1
        self._active_result_request = -1
        selected_id = (
            self._current_project().id
            if self._current_project() is not None
            else None
        )
        query = self._task_query()
        page = PageRequest(
            page=self._project_page.page,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        self.info_line.setText("正在加载任务，请稍候…")
        self._pending_project_selection = selected_id
        self._active_project_request = self.query_runner.submit(
            lambda: (
                self.repository.list_board_projects_page(
                    self.actor,
                    query.statuses,
                    query,
                    page,
                )
            )
        )

    def _request_task_page(
        self,
        project: ProjectRecord,
        *,
        preserve_selection: bool = True,
    ) -> None:
        self._active_result_request = -1
        current = self._current_task()
        self._pending_task_selection = (
            current.id if preserve_selection and current is not None else None
        )
        self.tasks = []
        self._tasks_by_id = {}
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(0)
        self.task_table.blockSignals(False)
        self.task_pagination.set_result(0, 1, 100)
        self.reference_gallery.clear()
        self.result_gallery.clear()
        self._update_actions()
        query = self._task_query()
        page = PageRequest(
            page=self._task_page.page,
            page_size=100,
            sort_field=self._task_sort_field,
            sort_direction=self._task_sort_direction,
        )
        self.info_line.setText("正在加载子任务，请稍候…")
        self._active_task_request = self.query_runner.submit(
            lambda: self.repository.list_board_tasks_for_project_page(
                project.id,
                self.actor,
                query.statuses,
                query,
                page,
            )
        )

    def _query_succeeded(self, request_id: int, result) -> None:
        if not self.async_mode:
            return
        if request_id == getattr(self, "_active_project_request", -1):
            self._render_project_page(result)
            return
        if request_id == getattr(self, "_active_task_request", -1):
            self._render_task_page(result)
            return
        if request_id == getattr(self, "_active_result_request", -1):
            task = self._current_task()
            if task is not None and task.id == self._active_result_task_id:
                self.result_gallery.set_task(task, result)
                self._update_preview_counts()
                self._update_actions()

    def _query_failed(self, request_id: int, message: str) -> None:
        if not self.async_mode or request_id not in (
            getattr(self, "_active_project_request", -1),
            getattr(self, "_active_task_request", -1),
            getattr(self, "_active_result_request", -1),
        ):
            return
        self.info_line.setText(f"加载失败：{message}")
        QMessageBox.warning(self, "读取任务失败", message)
        self._update_actions()

    def _render_project_page(
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
            self._request_project_page()
            return
        self.projects = list(result.items)
        self._projects_by_id = {
            project.id: project for project in self.projects
        }
        self._project_total = result.total
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
                project.owner_name,
                project.source_date.isoformat()
                if project.source_date
                else "",
                PROJECT_STATUS_LABELS.get(project.status, project.status),
                f"{project.approved_count}/{project.task_count}",
                "是" if project.is_urgent else "否",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, project.id)
                self.project_table.setItem(row, column, item)
        self.project_table.blockSignals(False)
        self.project_pagination.set_result(
            result.total,
            result.page,
            result.page_size,
        )
        if not self.projects:
            self.tasks = []
            self.task_table.setRowCount(0)
            self._task_total = 0
            self.task_pagination.set_result(0, 1, 100)
            self.info_line.setText("当前状态下没有任务")
            self.reference_strip.clear()
            self.reference_preview.set_image(None)
            self.reference_preview.set_placeholder("尚无参考图")
            self.result_gallery.set_task(self._empty_task(), [])
            self._update_preview_counts()
            self._update_actions()
            return
        target_id = getattr(self, "_pending_project_selection", None)
        target_row = next(
            (
                row
                for row, project in enumerate(self.projects)
                if project.id == target_id
            ),
            0,
        )
        self.project_table.selectRow(target_row)
        self._project_selected()

    def _render_task_page(
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
                self._request_task_page(project)
            return
        current_id = getattr(self, "_pending_task_selection", None)
        self.tasks = list(result.items)
        self._tasks_by_id = {task.id: task for task in self.tasks}
        self._task_total = result.total
        self._task_page = PageRequest(
            page=result.page,
            page_size=result.page_size,
            sort_field=self._task_sort_field,
            sort_direction=self._task_sort_direction,
        )
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(0)
        project = self._current_project()
        for task in self.tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            values = (
                task.task_code,
                str(len(task.garment_assets)),
                task.assignee_name or "未分配",
                TASK_STATUS_LABELS.get(task.status, task.status),
                "是" if project and project.is_urgent else "否",
                utc_naive_to_local(task.created_at).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                utc_naive_to_local(
                    task.submitted_at
                    or task.returned_at
                    or task.created_at
                ).strftime("%Y-%m-%d %H:%M"),
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
                    if task.id == current_id
                ),
                0,
            )
            self.task_table.selectRow(target_row)
            self._task_selected()
            self._pending_task_selection = None
        else:
            self.info_line.setText("当前筛选条件下没有子任务")
            self.reference_strip.clear()
            self.reference_preview.set_image(None)
            self.reference_preview.set_placeholder("尚无参考图")
            self.result_gallery.clear()
            self._update_preview_counts()
            self._update_actions()

    def _filter_changed(self, *_args) -> None:
        self._project_page = PageRequest(
            page=1,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        self._task_page = PageRequest(
            page=1,
            page_size=100,
            sort_field=self._task_sort_field,
            sort_direction=self._task_sort_direction,
        )
        if not self.async_mode:
            self._apply_filters()
            return
        if self.sender() is self.search_edit:
            self._keyword_timer.start()
        else:
            self._async_filter_requested()

    def _async_filter_requested(self) -> None:
        if self.async_mode:
            self._request_project_page()

    def _project_page_requested(self, page: int) -> None:
        self._project_page = PageRequest(
            page=page,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        if self.async_mode:
            self._request_project_page()

    def _task_page_requested(self, page: int) -> None:
        self._task_page = PageRequest(
            page=page,
            page_size=100,
            sort_field=self._task_sort_field,
            sort_direction=self._task_sort_direction,
        )
        project = self._current_project()
        if self.async_mode and project is not None:
            self._request_task_page(project)

    def _project_sort_requested(self, column: int) -> None:
        fields = {
            0: "project_code",
            1: "product_code",
            2: "owner_name",
            3: "source_date",
            4: "status",
            5: "progress",
            6: "is_urgent",
        }
        sort_field = fields.get(column)
        if sort_field is None:
            return
        if sort_field == self._project_sort_field:
            self._project_sort_direction = (
                "asc"
                if self._project_sort_direction == "desc"
                else "desc"
            )
        else:
            self._project_sort_field = sort_field
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
        self._request_project_page()

    def _task_sort_requested(self, column: int) -> None:
        fields = {
            0: "task_code",
            1: "garment_count",
            2: "assignee_name",
            3: "status",
            4: "is_urgent",
            5: "created_at",
            6: "updated_at",
        }
        sort_field = fields.get(column)
        if sort_field is None:
            return
        if sort_field == self._task_sort_field:
            self._task_sort_direction = (
                "asc" if self._task_sort_direction == "desc" else "desc"
            )
        else:
            self._task_sort_field = sort_field
            self._task_sort_direction = "asc"
        self.task_table.horizontalHeader().setSortIndicator(
            column,
            Qt.SortOrder.AscendingOrder
            if self._task_sort_direction == "asc"
            else Qt.SortOrder.DescendingOrder,
        )
        self._task_page = PageRequest(
            page=1,
            page_size=100,
            sort_field=self._task_sort_field,
            sort_direction=self._task_sort_direction,
        )
        project = self._current_project()
        if project is not None:
            self._request_task_page(
                project,
                preserve_selection=False,
            )

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

    def _apply_filters(self, *_args) -> None:
        statuses = set(RECORD_STATUSES)
        keyword = self.search_edit.text().strip().casefold()
        start = self.start_date_edit.python_date()
        end = self.end_date_edit.python_date()
        matching_tasks: dict[int, list[ProjectTask]] = {}
        for task in self._all_tasks:
            if task.status not in statuses:
                continue
            created = utc_naive_to_local(task.created_at).date()
            if start is not None and created < start:
                continue
            if end is not None and created > end:
                continue
            matching_tasks.setdefault(task.project_id, []).append(task)
        filtered_projects = []
        for project in self._projects_all:
            project_tasks = matching_tasks.get(project.id, [])
            project_text = " ".join(
                (
                    project.project_code,
                    project.product_code or project.name,
                    project.owner_name,
                    project.note,
                )
            ).casefold()
            task_match = any(
                keyword in " ".join(
                    (
                        task.task_code,
                        task.product_code or task.project_name,
                        task.assignee_name,
                    )
                ).casefold()
                for task in project_tasks
            )
            if project_tasks and (not keyword or keyword in project_text or task_match):
                filtered_projects.append(project)
        self.projects = filtered_projects
        self._projects_by_id = {
            project.id: project for project in filtered_projects
        }
        self.project_table.setSortingEnabled(False)
        self.project_table.setRowCount(0)
        for project in self.projects:
            row = self.project_table.rowCount()
            self.project_table.insertRow(row)
            values = (
                project.project_code,
                project.product_code or project.name,
                project.owner_name,
                project.source_date.isoformat() if project.source_date else "",
                PROJECT_STATUS_LABELS.get(project.status, project.status),
                f"{project.approved_count}/{project.task_count}",
                "是" if project.is_urgent else "否",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, project.id)
                self.project_table.setItem(row, column, item)
        self.project_table.setSortingEnabled(True)
        if self.projects:
            self.project_table.selectRow(0)
        else:
            self.tasks = []
            self.task_table.setRowCount(0)
            self.info_line.setText("当前状态下没有任务")
            self.reference_strip.clear()
            self.reference_preview.set_image(None)
            self.reference_preview.set_placeholder("尚无参考图")
            self.result_gallery.set_task(
                self._empty_task(),
                [],
            )
            self._update_preview_counts()
        self._update_actions()

    def _current_project(self) -> ProjectRecord | None:
        row = self.project_table.currentRow()
        item = self.project_table.item(row, 0) if row >= 0 else None
        if item is not None:
            return self._projects_by_id.get(
                int(item.data(Qt.ItemDataRole.UserRole))
            )
        return None

    def _project_selected(self) -> None:
        project = self._current_project()
        if project is None:
            return
        if self.async_mode:
            self._task_page = PageRequest(
                page=1,
                page_size=100,
                sort_field=self._task_sort_field,
                sort_direction=self._task_sort_direction,
            )
            self._request_task_page(project)
            return
        statuses = set(RECORD_STATUSES)
        keyword = self.search_edit.text().strip().casefold()
        start = self.start_date_edit.python_date()
        end = self.end_date_edit.python_date()
        self.tasks = [
            task
            for task in self._all_tasks
            if task.project_id == project.id
            and task.status in statuses
            and self._task_matches(task, project, keyword, start, end)
        ]
        self._tasks_by_id = {task.id: task for task in self.tasks}
        self.task_table.setSortingEnabled(False)
        self.task_table.setRowCount(0)
        for task in self.tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            values = (
                task.task_code,
                str(len(task.garment_assets)),
                task.assignee_name or "未分配",
                TASK_STATUS_LABELS.get(task.status, task.status),
                "是" if project.is_urgent else "否",
                utc_naive_to_local(
                    task.created_at
                ).strftime("%Y-%m-%d %H:%M"),
                utc_naive_to_local(
                    task.submitted_at or task.returned_at or task.created_at
                ).strftime("%Y-%m-%d %H:%M"),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, task.id)
                self.task_table.setItem(row, column, item)
        self.task_table.setSortingEnabled(True)
        if self.tasks:
            self.task_table.selectRow(0)
            self._task_selected()
        else:
            self.info_line.setText(
                f"{project.product_code or project.name} · "
                f"{project.source_date or ''} · {project.owner_name} · "
                f"{project.note or '无备注'}"
            )
        self._update_actions()

    @staticmethod
    def _task_matches(
        task: ProjectTask,
        project: ProjectRecord,
        keyword: str,
        start,
        end,
    ) -> bool:
        created = utc_naive_to_local(task.created_at).date()
        if start is not None and created < start:
            return False
        if end is not None and created > end:
            return False
        if not keyword:
            return True
        searchable = " ".join(
            (
                task.task_code,
                task.product_code or task.project_name,
                task.assignee_name,
                project.project_code,
                project.product_code or project.name,
                project.owner_name,
                project.note,
            )
        ).casefold()
        return keyword in searchable

    def _current_task(self) -> ProjectTask | None:
        row = self.task_table.currentRow()
        item = self.task_table.item(row, 0) if row >= 0 else None
        if item is not None:
            return self._tasks_by_id.get(
                int(item.data(Qt.ItemDataRole.UserRole))
            )
        return None

    def _task_selected(self) -> None:
        task = self._current_task()
        project = self._current_project()
        if task is None or project is None:
            self.reference_strip.clear()
            self.reference_preview.set_image(None)
            self.reference_preview.set_placeholder("尚未选择任务")
            self.result_gallery.clear()
            self._update_preview_counts()
            self._update_actions()
            return
        self.info_line.setText(
            f"{project.product_code or project.name} · "
            f"{project.source_date or ''} · {project.owner_name} · "
            f"{project.note or '无备注'}"
        )
        self.info_line.setToolTip(project.project_relative_root)
        self._set_reference_preview(task)
        if self.async_mode:
            self.result_gallery.clear()
            self._active_result_task_id = task.id
            self._active_result_request = self.query_runner.submit(
                lambda task_id=task.id: self.repository.list_results(task_id)
            )
        else:
            self.result_gallery.set_task(
                task,
                self.repository.list_results(task.id),
            )
        self._update_preview_counts()
        self._update_actions()

    def _set_reference_preview(self, task: ProjectTask) -> None:
        items = []
        for index, asset in enumerate(task.garment_assets, start=1):
            path = self.resolver.resolve_project_path(
                task.project_relative_root,
                asset.relative_path,
            )
            items.append((path, f"衣服 {index}", path))
        model_path = self.resolver.resolve_project_path(
            task.project_relative_root,
            task.model_path,
        )
        items.append((model_path, "模特", model_path))
        self.reference_gallery.set_images(
            items,
            default_row=len(items) - 1,
        )
        self._update_preview_counts()

    def _update_preview_counts(self) -> None:
        self.result_header.setText(
            f"结果图（{self.result_gallery.list_widget.count()}）"
        )
        self.reference_header.setText(
            f"参考图（{self.reference_strip.count()}）"
        )

    def _update_actions(self) -> None:
        return

    @staticmethod
    def _empty_task() -> ProjectTask:
        return ProjectTask(
            id=0,
            task_code="",
            project_id=0,
            project_code="",
            project_name="",
            project_relative_root="",
            garment_asset_id=0,
            garment_path="",
            garment_name="",
            garment_hash="",
            model_asset_id=0,
            model_path="",
            model_name="",
            model_hash="",
            status="",
            assignee_id=None,
            assignee_name="",
            prompt_text="",
            model="",
            aspect_ratio="3:4",
            resolution="2K",
            n=1,
            final_quantity=0,
            owner_id=0,
            created_at=utc_naive_to_local(utc_now()),
        )
