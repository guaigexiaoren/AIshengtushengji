from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
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
    PROJECT_AWAITING_ACCEPTANCE,
    PROJECT_CANCELLED,
    PROJECT_COMPLETED,
    PROJECT_IN_PROGRESS,
    PROJECT_SCOPE_LABELS,
    PROJECT_SKIPPED,
    PROJECT_STATUS_LABELS,
    PROJECT_SUBMITTED,
    PROJECT_TASK_TYPE_BUYER_SHOW,
    PROJECT_TASK_TYPE_LABELS,
    PROJECT_TASK_TYPES,
    ROLE_ADMIN,
    AuthUser,
    ProjectRecord,
)
from ..time_utils import utc_naive_to_local
from ..paging import PageRequest, PageResult, TaskQuery
from .async_queries import get_query_runner
from .components import FilterBar
from .date_filter import OptionalDateEdit
from .dialogs import ListingConfirmDialog
from .image_preview import ImagePreviewPane
from .pagination_bar import PaginationBar
from .widgets import scan_image_inputs

VIEW_TITLES = {
    "records": (
        "提交记录",
        "只读查看主任务，可查看共享盘 AI 成品并确认上架。",
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
        self._project_page = PageRequest()
        self._project_total = 0
        self._project_sort_field = "created_at"
        self._project_sort_direction = "desc"
        self._active_project_request = -1
        self._active_ai_request = -1
        self._active_ai_project_id: int | None = None
        self._pending_project_selection: int | None = None
        self._current_ai_folder: Path | None = None
        self._keyword_timer = QTimer(self)
        self._keyword_timer.setSingleShot(True)
        self._keyword_timer.setInterval(350)
        self._keyword_timer.timeout.connect(
            self._async_filter_requested
        )
        self.projects: list[ProjectRecord] = []
        self._projects_by_id: dict[int, ProjectRecord] = {}
        self._info_labels: dict[str, QLabel] = {}
        self._build()
        if self.query_runner is not None:
            self.query_runner.succeeded.connect(self._query_succeeded)
            self.query_runner.failed.connect(self._query_failed)
        if auto_load:
            self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QFrame()
        left.setObjectName("Card")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(10, 10, 10, 10)
        filter_bar = FilterBar()
        filters = filter_bar.layout
        refresh = QPushButton("刷新")
        refresh.setObjectName("SecondaryButton")
        refresh.clicked.connect(self.refresh)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "搜索任务编码、货号、项目名称、运营或备注"
        )
        self.range_combo = QComboBox()
        for label, value in (
            ("全部", "all"),
            ("近实时", "today"),
            ("近7天", "7d"),
            ("近30天", "30d"),
            ("本周", "week"),
            ("本月", "month"),
            ("自定义", "custom"),
        ):
            self.range_combo.addItem(label, value)
        self.status_combo = QComboBox()
        self.status_combo.addItem("全部状态", None)
        for value in (
            PROJECT_SUBMITTED,
            PROJECT_IN_PROGRESS,
            PROJECT_AWAITING_ACCEPTANCE,
            PROJECT_COMPLETED,
            PROJECT_SKIPPED,
            PROJECT_CANCELLED,
        ):
            self.status_combo.addItem(
                PROJECT_STATUS_LABELS[value],
                value,
            )
        self.listing_combo = QComboBox()
        self.listing_combo.addItem("全部上架状态", None)
        self.listing_combo.addItem("未上架", "unlisted")
        self.listing_combo.addItem("已上架", "listed")
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部类型", None)
        for value in PROJECT_TASK_TYPES:
            self.type_combo.addItem(
                PROJECT_TASK_TYPE_LABELS[value],
                value,
            )
        self.start_date_edit = self._optional_date_edit("开始日期")
        self.end_date_edit = self._optional_date_edit("结束日期")
        self.start_date_edit.setVisible(False)
        self.end_date_edit.setVisible(False)
        reset_filter = QPushButton("重置筛选")
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(QLabel("任务状态"))
        filters.addWidget(self.status_combo)
        filters.addWidget(QLabel("上架状态"))
        filters.addWidget(self.listing_combo)
        filters.addWidget(QLabel("类型"))
        filters.addWidget(self.type_combo)
        filters.addWidget(QLabel("提交时间"))
        filters.addWidget(self.range_combo)
        filters.addWidget(self.start_date_edit)
        filters.addWidget(QLabel("至"))
        filters.addWidget(self.end_date_edit)
        filters.addWidget(reset_filter)
        filters.addWidget(refresh)
        left_layout.addWidget(filter_bar)

        self.project_table = QTableWidget(0, 13)
        self.project_table.setHorizontalHeaderLabels(
            [
                "任务编码",
                "货号",
                "类型",
                "运营",
                "制作人员",
                "任务状态",
                "子任务（作图中）",
                "成品数",
                "上架状态",
                "上架链接ID",
                "提交时间",
                "完成时间",
                "加急",
            ]
        )
        self.project_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.project_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.project_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.project_table.setSortingEnabled(False)
        header_view = self.project_table.horizontalHeader()
        header_view.setStretchLastSection(True)
        header_view.setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        header_view.setSortIndicatorShown(True)
        header_view.setSortIndicator(10, Qt.SortOrder.DescendingOrder)
        header_view.sectionClicked.connect(self._project_sort_requested)
        for column, width in enumerate(
            (190, 130, 80, 100, 130, 90, 90, 70, 80, 150, 140, 140, 60)
        ):
            self.project_table.setColumnWidth(column, width)
        self.project_table.setColumnHidden(12, True)
        self.project_table.itemSelectionChanged.connect(
            self._project_selected
        )
        left_layout.addWidget(self.project_table, 1)
        self.project_pagination = PaginationBar()
        self.project_pagination.page_requested.connect(
            self._project_page_requested
        )
        left_layout.addWidget(self.project_pagination)
        splitter.addWidget(left)

        right = QFrame()
        right.setObjectName("Card")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.addWidget(self._build_info_panel())
        right_layout.addWidget(self._build_ai_panel(), 1)
        splitter.addWidget(right)
        splitter.setSizes([900, 600])
        outer.addWidget(splitter, 1)

        self.search_edit.textChanged.connect(self._filter_changed)
        self.status_combo.currentIndexChanged.connect(self._filter_changed)
        self.listing_combo.currentIndexChanged.connect(self._filter_changed)
        self.type_combo.currentIndexChanged.connect(self._filter_changed)
        self.range_combo.currentIndexChanged.connect(self._range_changed)
        self.start_date_edit.dateChanged.connect(self._filter_changed)
        self.end_date_edit.dateChanged.connect(self._filter_changed)
        reset_filter.clicked.connect(self._reset_filters)

    @staticmethod
    def _optional_date_edit(placeholder: str) -> OptionalDateEdit:
        return OptionalDateEdit(placeholder)

    def _build_info_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        group = QGroupBox("主任务信息")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        fields = (
            ("product_code", "货号"),
            ("project_name", "项目名称"),
            ("scope", "项目类型"),
            ("task_type", "任务类型"),
            ("owner", "运营"),
            ("assignees", "制作人员"),
            ("source_date", "来源日期"),
            ("path", "共享盘路径"),
            ("note", "备注"),
            ("listing_status", "上架状态"),
            ("listing_url", "上架链接"),
            ("listing_id", "上架链接ID"),
            ("listing_time", "确认时间"),
        )
        for row, (key, title) in enumerate(fields):
            label_title = QLabel(title)
            label_title.setStyleSheet("color:#60708a;")
            value = QLabel("-")
            value.setWordWrap(True)
            value.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            grid.addWidget(label_title, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
            self._info_labels[key] = value
        grid.setColumnStretch(1, 1)
        layout.addWidget(group)
        self.listing_button = QPushButton("确认上架")
        self.listing_button.setObjectName("PrimaryButton")
        self.listing_button.setMinimumHeight(46)
        self.listing_button.setEnabled(False)
        self.listing_button.clicked.connect(self._confirm_listing)
        layout.addWidget(self.listing_button)
        return panel

    def _build_ai_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.ai_preview = ImagePreviewPane(
            "ai_result",
            "AI成品",
            resolver=self.resolver,
            async_thumbnails=self.async_mode,
        )
        layout.addWidget(self.ai_preview, 1)
        path_row = QHBoxLayout()
        self.ai_path_label = QLabel("")
        self.ai_path_label.setWordWrap(True)
        self.ai_path_label.setStyleSheet("color:#60708a;")
        self.ai_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        open_button = QPushButton("打开文件夹")
        open_button.clicked.connect(self._open_ai_folder)
        path_row.addWidget(self.ai_path_label, 1)
        path_row.addWidget(open_button)
        layout.addLayout(path_row)
        return panel

    def refresh(self) -> None:
        try:
            shared_root = self.repository.get_shared_setting("share_root")
        except Exception:
            shared_root = None
        if shared_root:
            self.resolver = SharedPathResolver(shared_root)
            self.ai_preview.set_resolver(self.resolver)
        current = self._current_project()
        self._pending_project_selection = current.id if current else None
        self._project_page = PageRequest(
            page=1,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        if self.async_mode:
            self._request_project_page()
            return
        self._load_projects_sync()

    def _date_range(self) -> tuple[date | None, date | None]:
        value = self.range_combo.currentData()
        today = date.today()
        if value == "today":
            return today, today
        if value == "7d":
            return today - timedelta(days=6), today
        if value == "30d":
            return today - timedelta(days=29), today
        if value == "week":
            return today - timedelta(days=today.weekday()), today
        if value == "month":
            return today.replace(day=1), today
        if value == "custom":
            return (
                self.start_date_edit.python_date(),
                self.end_date_edit.python_date(),
            )
        return None, None

    def _range_changed(self, _index: int) -> None:
        custom = self.range_combo.currentData() == "custom"
        self.start_date_edit.setVisible(custom)
        self.end_date_edit.setVisible(custom)
        self._filter_changed()

    def _query(self) -> TaskQuery:
        start_date, end_date = self._date_range()
        status = self.status_combo.currentData()
        return TaskQuery(
            keyword=self.search_edit.text().strip(),
            statuses=(status,) if status else (),
            start_date=start_date,
            end_date=end_date,
            listing_status=self.listing_combo.currentData(),
            task_type=self.type_combo.currentData(),
        )

    def _request_project_page(self) -> None:
        query = self._query()
        page = self._project_page
        self._active_project_request = self.query_runner.submit(
            lambda: self.repository.list_submission_projects_page(
                self.actor,
                query,
                page,
            )
        )

    def _load_projects_sync(self) -> None:
        try:
            result = self.repository.list_submission_projects_page(
                self.actor,
                self._query(),
                self._project_page,
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            return
        self._render_project_page(result)

    def _query_succeeded(self, request_id: int, result) -> None:
        if not self.async_mode:
            return
        if request_id == self._active_project_request:
            self._render_project_page(result)
            return
        if request_id == self._active_ai_request:
            project = self._current_project()
            if (
                project is not None
                and project.id == self._active_ai_project_id
            ):
                items, folder, errors = result
                self._render_ai_results(items, folder, errors)

    def _query_failed(self, request_id: int, message: str) -> None:
        if not self.async_mode or request_id not in (
            self._active_project_request,
            self._active_ai_request,
        ):
            return
        QMessageBox.warning(self, "读取任务失败", message)

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
            if self.async_mode:
                self._request_project_page()
            else:
                self._load_projects_sync()
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
                PROJECT_TASK_TYPE_LABELS.get(
                    project.task_type,
                    project.task_type,
                ),
                project.owner_name,
                project.assignee_names or "未分配",
                PROJECT_STATUS_LABELS.get(project.status, project.status),
                (
                    f"{project.approved_count}/{project.task_count}"
                    if project.task_count
                    else "未拆分"
                ),
                str(project.result_count),
                self._listing_status_text(project),
                project.listing_id,
                utc_naive_to_local(project.created_at).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                (
                    utc_naive_to_local(project.completed_at).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if project.completed_at
                    else ""
                ),
                "是" if project.is_urgent else "否",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        project.id,
                    )
                self.project_table.setItem(row, column, item)
        self.project_table.blockSignals(False)
        self.project_pagination.set_result(
            result.total,
            result.page,
            result.page_size,
        )
        if not self.projects:
            self._clear_details()
            return
        target_id = self._pending_project_selection
        target_row = next(
            (
                row
                for row, project in enumerate(self.projects)
                if project.id == target_id
            ),
            0,
        )
        self.project_table.blockSignals(True)
        self.project_table.selectRow(target_row)
        self.project_table.blockSignals(False)
        self._project_selected()
        self._pending_project_selection = None

    def _filter_changed(self, *_args) -> None:
        self._project_page = PageRequest(
            page=1,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        self._pending_project_selection = None
        if not self.async_mode:
            self._load_projects_sync()
            return
        if self.sender() is self.search_edit:
            self._keyword_timer.start()
        else:
            self._async_filter_requested()

    def _async_filter_requested(self) -> None:
        if self.async_mode:
            self._pending_project_selection = None
            self._request_project_page()

    def _project_page_requested(self, page: int) -> None:
        self._project_page = PageRequest(
            page=page,
            page_size=100,
            sort_field=self._project_sort_field,
            sort_direction=self._project_sort_direction,
        )
        self._pending_project_selection = None
        if self.async_mode:
            self._request_project_page()
        else:
            self._load_projects_sync()

    def _project_sort_requested(self, column: int) -> None:
        fields = {
            0: "project_code",
            1: "product_code",
            2: "task_type",
            3: "owner_name",
            5: "status",
            6: "progress",
            10: "created_at",
            11: "completed_at",
            12: "is_urgent",
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
        current = self._current_project()
        self._pending_project_selection = current.id if current else None
        if self.async_mode:
            self._request_project_page()
        else:
            self._load_projects_sync()

    def _reset_filters(self) -> None:
        for widget in (
            self.search_edit,
            self.status_combo,
            self.listing_combo,
            self.type_combo,
            self.range_combo,
            self.start_date_edit,
            self.end_date_edit,
        ):
            widget.blockSignals(True)
        self.search_edit.clear()
        self.status_combo.setCurrentIndex(0)
        self.listing_combo.setCurrentIndex(0)
        self.type_combo.setCurrentIndex(0)
        self.range_combo.setCurrentIndex(0)
        self.start_date_edit.clear()
        self.end_date_edit.clear()
        self.start_date_edit.setVisible(False)
        self.end_date_edit.setVisible(False)
        for widget in (
            self.search_edit,
            self.status_combo,
            self.listing_combo,
            self.type_combo,
            self.range_combo,
            self.start_date_edit,
            self.end_date_edit,
        ):
            widget.blockSignals(False)
        self._filter_changed()

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
            self._clear_details()
            return
        self._render_project_info(project)
        self._request_ai_results(project)

    @staticmethod
    def _is_buyer_show(project: ProjectRecord) -> bool:
        return project.task_type == PROJECT_TASK_TYPE_BUYER_SHOW

    @classmethod
    def _listing_status_text(cls, project: ProjectRecord) -> str:
        if project.listing_confirmed_at:
            return "已上架"
        if cls._is_buyer_show(project):
            return "无需上架"
        return "未上架"

    def _set_info(self, key: str, value: str) -> None:
        label = self._info_labels.get(key)
        if label is not None:
            label.setText(value or "-")

    def _render_project_info(self, project: ProjectRecord) -> None:
        self._set_info(
            "product_code",
            project.product_code or project.name,
        )
        self._set_info("project_name", project.project_name or project.name)
        self._set_info(
            "scope",
            PROJECT_SCOPE_LABELS.get(
                project.project_scope,
                project.project_scope,
            ),
        )
        self._set_info("owner", project.owner_name)
        self._set_info(
            "task_type",
            PROJECT_TASK_TYPE_LABELS.get(
                project.task_type,
                project.task_type,
            ),
        )
        self._set_info("assignees", project.assignee_names or "未分配")
        self._set_info(
            "source_date",
            project.source_date.isoformat()
            if project.source_date
            else "",
        )
        self._set_info("path", project.project_relative_root)
        self._set_info("note", project.note or "无备注")
        if self._is_buyer_show(project):
            self._set_info("listing_status", "无需上架")
            self._set_info("listing_url", "")
            self._set_info("listing_id", "")
            self._set_info("listing_time", "")
            self.listing_button.setVisible(False)
            self.listing_button.setEnabled(False)
            self.listing_button.setToolTip("")
            return
        self.listing_button.setVisible(True)
        if project.listing_confirmed_at:
            self._set_info("listing_status", "已上架")
            self._set_info("listing_url", project.listing_url)
            self._set_info("listing_id", project.listing_id or "未识别到")
            self._set_info(
                "listing_time",
                utc_naive_to_local(
                    project.listing_confirmed_at
                ).strftime("%Y-%m-%d %H:%M"),
            )
        else:
            self._set_info("listing_status", "未上架")
            self._set_info("listing_url", "")
            self._set_info("listing_id", "")
            self._set_info("listing_time", "")
        can_confirm = (
            project.status == PROJECT_COMPLETED
            and (
                self.actor.role == ROLE_ADMIN
                or project.owner_id == self.actor.id
            )
        )
        self.listing_button.setEnabled(can_confirm)
        self.listing_button.setText(
            "修改上架信息" if project.listing_confirmed_at else "确认上架"
        )
        self._apply_listing_button_style(bool(project.listing_confirmed_at))
        if project.status != PROJECT_COMPLETED:
            self.listing_button.setToolTip("主任务完成后才能确认上架")
        elif not can_confirm:
            self.listing_button.setToolTip("只有提交该任务的运营或管理员可以确认上架")
        else:
            self.listing_button.setToolTip("")

    def _apply_listing_button_style(self, listed: bool) -> None:
        name = "SecondaryButton" if listed else "PrimaryButton"
        if self.listing_button.objectName() == name:
            return
        self.listing_button.setObjectName(name)
        self.listing_button.style().unpolish(self.listing_button)
        self.listing_button.style().polish(self.listing_button)

    def _clear_details(self) -> None:
        for label in self._info_labels.values():
            label.setText("-")
        self.listing_button.setEnabled(False)
        self.listing_button.setVisible(False)
        self.listing_button.setText("确认上架")
        self.ai_preview.clear()
        self.ai_path_label.setText("")
        self._current_ai_folder = None
        self._active_ai_request = -1
        self._active_ai_project_id = None

    def _request_ai_results(self, project: ProjectRecord) -> None:
        if self.async_mode:
            self.ai_preview.clear()
            self._active_ai_project_id = project.id
            root = project.project_relative_root
            self._active_ai_request = self.query_runner.submit(
                lambda: self._scan_ai_folder(root)
            )
            return
        items, folder, errors = self._scan_ai_folder(
            project.project_relative_root
        )
        self._render_ai_results(items, folder, errors)

    def _scan_ai_folder(
        self,
        project_relative_root: str,
    ) -> tuple[list[tuple[Path, str, object]], Path, list[str]]:
        folder = (
            self.resolver.project_root(project_relative_root) / "AI成品"
        )
        paths, errors = scan_image_inputs([folder])
        items = [
            (path, f"成品 {index + 1}", path)
            for index, path in enumerate(paths)
        ]
        return items, folder, errors

    def _render_ai_results(
        self,
        items: list[tuple[Path, str, object]],
        folder: Path,
        errors: list[str],
    ) -> None:
        self._current_ai_folder = folder
        self.ai_path_label.setText(str(folder))
        self.ai_preview.set_images(items)
        if errors and not items:
            self.ai_path_label.setText(
                str(folder) + "（" + "；".join(errors) + "）"
            )

    def _open_ai_folder(self) -> None:
        if self._current_ai_folder is None:
            return
        folder = self._current_ai_folder
        if not folder.is_dir():
            QMessageBox.information(
                self,
                "文件夹不存在",
                f"{folder}\n还没有生成 AI 成品。",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _confirm_listing(self) -> None:
        project = self._current_project()
        if project is None:
            return
        dialog = ListingConfirmDialog(project.listing_url, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.repository.confirm_project_listing(
                project.id,
                self.actor,
                dialog.listing_url(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "确认上架失败", str(exc))
            return
        self._pending_project_selection = project.id
        if self.async_mode:
            self._request_project_page()
        else:
            self._load_projects_sync()
