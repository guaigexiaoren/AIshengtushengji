from __future__ import annotations

from PySide6.QtCore import Qt
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    ATTEMPT_FAILED,
    ATTEMPT_QUEUED,
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    ROLE_AI,
    ROLE_ADMIN,
    AuthUser,
)
from ..constants import MODEL_LABELS
from ..time_utils import utc_naive_to_local
from .result_gallery import ResultGallery
from .date_filter import OptionalDateEdit


ATTEMPT_LABELS = {
    ATTEMPT_QUEUED: "排队中",
    ATTEMPT_RUNNING: "生成中",
    ATTEMPT_SUCCEEDED: "成功",
    ATTEMPT_FAILED: "失败",
    "manual_ps": "PS成品",
}


class ProjectHistoryPage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
    ) -> None:
        super().__init__(parent)
        if actor.role not in (ROLE_AI, ROLE_ADMIN):
            raise ValueError("当前角色不能查看项目生成历史")
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.attempts: list[dict] = []
        self._attempts_by_id: dict[int, dict] = {}
        self.current_attempt_id: int | None = None
        self._build()
        if auto_load:
            self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        filters = QHBoxLayout()
        title = QLabel("项目任务历史")
        title.setStyleSheet("font-size:20px;font-weight:700;")
        self.project_filter = QComboBox()
        self.project_filter.addItem("全部项目", None)
        self.product_search = QLineEdit()
        self.product_search.setPlaceholderText("搜索货号")
        self.status_filter = QComboBox()
        self.status_filter.addItem("全部状态", None)
        for status, label in ATTEMPT_LABELS.items():
            self.status_filter.addItem(label, status)
        self.start_date = self._optional_date_edit("开始日期")
        self.end_date = self._optional_date_edit("结束日期")
        reset = QPushButton("重置筛选")
        refresh = QPushButton("刷新")
        filters.addWidget(title)
        filters.addWidget(self.project_filter, 1)
        filters.addWidget(self.product_search, 1)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.start_date)
        filters.addWidget(self.end_date)
        filters.addWidget(reset)
        filters.addWidget(refresh)
        outer.addLayout(filters)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            [
                "项目编码",
                "子任务编码",
                "货号",
                "操作人员",
                "模型",
                "API 平台",
                "状态",
                "创建时间",
                "结果数",
                "提示词",
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
            (220, 240, 90, 120, 130, 110, 90, 145, 70, 260)
        ):
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self._attempt_selected)
        splitter.addWidget(self.table)

        detail = QFrame()
        detail.setObjectName("Card")
        detail_layout = QVBoxLayout(detail)
        self.detail_title = QLabel("选择一个生成轮次")
        self.detail_title.setStyleSheet("font-size:18px;font-weight:700;")
        detail_layout.addWidget(self.detail_title)
        self.detail_meta = QLabel()
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setStyleSheet("color:#60708a;")
        detail_layout.addWidget(self.detail_meta)
        self.prompt_view = QTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setMaximumHeight(150)
        detail_layout.addWidget(self.prompt_view)
        self.gallery = ResultGallery(self.resolver)
        detail_layout.addWidget(self.gallery, 1)
        splitter.addWidget(detail)
        splitter.setSizes([760, 520])
        outer.addWidget(splitter, 1)

        self.project_filter.currentIndexChanged.connect(self._apply_filters)
        self.product_search.textChanged.connect(self._apply_filters)
        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        self.start_date.dateChanged.connect(self._apply_filters)
        self.end_date.dateChanged.connect(self._apply_filters)
        reset.clicked.connect(self._reset_filters)
        refresh.clicked.connect(self.refresh)

    @staticmethod
    def _optional_date_edit(placeholder: str) -> OptionalDateEdit:
        return OptionalDateEdit(placeholder)

    def refresh(self) -> None:
        try:
            shared_root = self.repository.get_shared_setting("share_root")
            if shared_root:
                self.resolver = SharedPathResolver(shared_root)
                self.gallery.resolver = self.resolver
            self.attempts = self.repository.list_project_attempt_history(
                self.actor
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取项目历史失败", str(exc))
            return
        self._refresh_project_filter()
        self._apply_filters()

    def _refresh_project_filter(self) -> None:
        selected = self.project_filter.currentData()
        projects = {
            self._attempt_project_name(row)
            for row in self.attempts
        }
        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem("全部项目", None)
        for project_name in sorted(projects):
            self.project_filter.addItem(project_name, project_name)
        index = self.project_filter.findData(selected)
        self.project_filter.setCurrentIndex(max(0, index))
        self.project_filter.blockSignals(False)

    def _reset_filters(self) -> None:
        self.project_filter.setCurrentIndex(0)
        self.product_search.clear()
        self.status_filter.setCurrentIndex(0)
        self.start_date.clear()
        self.end_date.clear()
        self._apply_filters()

    def _apply_filters(self, *_args) -> None:
        project_name = self.project_filter.currentData()
        product = self.product_search.text().strip().casefold()
        status = self.status_filter.currentData()
        start = self.start_date.python_date()
        end = self.end_date.python_date()
        rows = []
        for attempt in self.attempts:
            created = utc_naive_to_local(attempt["created_at"]).date()
            if (
                project_name
                and self._attempt_project_name(attempt) != project_name
            ):
                continue
            if product and product not in str(
                attempt["product_code"] or ""
            ).casefold():
                continue
            if status and attempt["status"] != status:
                continue
            if start is not None and created < start:
                continue
            if end is not None and created > end:
                continue
            rows.append(attempt)
        self._attempts_by_id = {
            int(attempt["id"]): attempt for attempt in rows
        }
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for attempt in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                attempt["project_code"],
                attempt["task_code"],
                attempt["product_code"] or attempt["project_name"],
                attempt["operator_name"],
                MODEL_LABELS.get(attempt["model"], attempt["model"]),
                attempt.get("provider_name") or "自动/历史",
                ATTEMPT_LABELS.get(attempt["status"], attempt["status"]),
                utc_naive_to_local(attempt["created_at"]).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                str(attempt["result_count"]),
                str(attempt["prompt_text"]).replace("\n", " ")[:80],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, attempt["id"])
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        if self.table.rowCount():
            target = 0
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item and item.data(Qt.ItemDataRole.UserRole) == (
                    self.current_attempt_id
                ):
                    target = row
                    break
            self.table.selectRow(target)
        else:
            self.current_attempt_id = None
            self.detail_title.setText("没有符合条件的生成记录")
            self.detail_meta.clear()
            self.prompt_view.clear()
            self.gallery.clear()

    @staticmethod
    def _attempt_project_name(attempt: dict) -> str:
        return str(
            attempt.get("access_project_name")
            or attempt.get("product_code")
            or attempt.get("project_code")
            or ""
        ).strip()

    def _attempt_selected(self) -> None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        attempt_id = (
            int(item.data(Qt.ItemDataRole.UserRole))
            if item is not None
            else None
        )
        attempt = self._attempts_by_id.get(attempt_id)
        if attempt is None:
            return
        self.current_attempt_id = attempt_id
        self.detail_title.setText(
            f"{attempt['task_code']} · 第 {attempt['attempt_no']} 轮"
        )
        self.detail_meta.setText(
            f"项目 {attempt['project_code']} · 货号 "
            f"{attempt['product_code'] or attempt['project_name']}\n"
            f"操作人员 {attempt['operator_name']} · 模型 "
            f"{MODEL_LABELS.get(attempt['model'], attempt['model'])} · "
            f"API 平台 {attempt.get('provider_name') or '自动/历史'} · "
            f"状态 {ATTEMPT_LABELS.get(attempt['status'], attempt['status'])}"
        )
        self.prompt_view.setPlainText(attempt["prompt_text"])
        task = self.repository.get_task(int(attempt["task_id"]))
        if task is None:
            self.gallery.clear()
            return
        try:
            results = self.repository.list_attempt_results(attempt_id)
        except Exception as exc:
            QMessageBox.warning(self, "读取结果失败", str(exc))
            results = []
        self.gallery.set_task(task, results)
