from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QHBoxLayout,
    QLabel,
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
    TASK_STATUS_LABELS,
    AuthUser,
    ProjectRecord,
    ProjectTask,
)
from ..time_utils import utc_naive_to_local
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
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.projects: list[ProjectRecord] = []
        self.tasks: list[ProjectTask] = []
        self._projects_by_id: dict[int, ProjectRecord] = {}
        self._tasks_by_id: dict[int, ProjectTask] = {}
        self._build()
        if auto_load:
            self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("待分配")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel(
            "选择完整未分配的主任务并整组领取；任一子任务已被领取时整批取消。"
        )
        subtitle.setStyleSheet("color:#6d7a91;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.claim_button = QPushButton("领取所选")
        self.claim_button.setObjectName("PrimaryButton")
        self.claim_button.setEnabled(False)
        self.claim_button.clicked.connect(self._claim_selected)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        header.addWidget(self.claim_button)
        header.addWidget(refresh)
        outer.addLayout(header)

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

    def refresh(self) -> None:
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

    def _selection_changed(self) -> None:
        selected_ids = self._selected_project_ids()
        self.claim_button.setText(
            f"领取所选（{len(selected_ids)}）"
            if selected_ids
            else "领取所选"
        )
        self.claim_button.setEnabled(bool(selected_ids))
        row = self.project_table.currentRow()
        item = self.project_table.item(row, 0) if row >= 0 else None
        project_id = (
            int(item.data(Qt.ItemDataRole.UserRole))
            if item is not None
            else None
        )
        if project_id is not None:
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
