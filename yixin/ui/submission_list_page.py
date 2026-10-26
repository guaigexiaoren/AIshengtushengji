from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
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
    PROJECT_STATUS_LABELS,
    ROLE_ADMIN,
    TASK_APPROVED,
    TASK_AVAILABLE,
    TASK_AWAITING_ACCEPTANCE,
    TASK_CLAIMED,
    TASK_RETURNED,
    TASK_STATUS_LABELS,
    TASK_WORKING,
    AuthUser,
    ProjectRecord,
    ProjectTask,
)
from ..time_utils import utc_naive_to_local
from .result_gallery import ResultGalleryDialog
from .subtask_source_dialog import SubtaskSourceDialog
from .widgets import load_scaled_pixmap


VIEW_STATUSES = {
    "submitted": (TASK_AVAILABLE, TASK_CLAIMED, TASK_WORKING, TASK_RETURNED),
    "pending": (TASK_AWAITING_ACCEPTANCE,),
    "completed": (TASK_APPROVED,),
}
VIEW_TITLES = {
    "submitted": ("已提交", "包含可领取、制作中和已退回的子任务。"),
    "pending": ("待审核", "查看参考图与成品，通过或填写修改点退回。"),
    "completed": ("已完成", "查看已经通过验收的子任务与成品。"),
}


class SubmissionListPage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        view_mode: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.view_mode = view_mode
        self.projects: list[ProjectRecord] = []
        self.tasks: list[ProjectTask] = []
        self._all_tasks: list[ProjectTask] = []
        self._build()
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        title_text, subtitle_text = VIEW_TITLES[self.view_mode]
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel(title_text)
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel(subtitle_text)
        subtitle.setStyleSheet("color:#6d7a91;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        outer.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
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
        self.project_table.itemSelectionChanged.connect(self._project_selected)
        splitter.addWidget(self.project_table)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        self.detail_label = QLabel("选择一个任务查看子任务")
        self.detail_label.setWordWrap(True)
        detail_layout.addWidget(self.detail_label)
        self.task_table = QTableWidget(0, 7)
        self.task_table.setHorizontalHeaderLabels(
            ["子任务编码", "模特图", "衣服数", "制作人员", "状态", "加急", "修改时间"]
        )
        self.task_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.task_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.task_table.horizontalHeader().setStretchLastSection(True)
        self.task_table.itemSelectionChanged.connect(self._task_selected)
        detail_layout.addWidget(self.task_table, 1)
        self.reference_list = QListWidget()
        self.reference_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.reference_list.setIconSize(QSize(82, 82))
        self.reference_list.setGridSize(QSize(112, 108))
        self.reference_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.reference_list.setMinimumHeight(120)
        detail_layout.addWidget(self.reference_list)
        actions = QHBoxLayout()
        self.edit_sources = QPushButton("修改素材与配对")
        self.urgent_button = QPushButton("设置加急")
        self.view_results = QPushButton("查看/对比成品")
        self.return_button = QPushButton("退回返工")
        self.approve_button = QPushButton("验收通过")
        self.approve_button.setObjectName("PrimaryButton")
        self.edit_sources.clicked.connect(self._edit_sources)
        self.urgent_button.clicked.connect(self._toggle_urgent)
        self.view_results.clicked.connect(self._show_results)
        self.return_button.clicked.connect(self._return_task)
        self.approve_button.clicked.connect(self._approve_task)
        actions.addWidget(self.edit_sources)
        actions.addWidget(self.urgent_button)
        actions.addStretch(1)
        actions.addWidget(self.view_results)
        actions.addWidget(self.return_button)
        actions.addWidget(self.approve_button)
        detail_layout.addLayout(actions)
        splitter.addWidget(detail)
        splitter.setSizes([350, 380])
        outer.addWidget(splitter, 1)
        self._update_actions()

    def refresh(self) -> None:
        try:
            shared_root = self.repository.get_shared_setting("share_root")
            if shared_root:
                self.resolver = SharedPathResolver(shared_root)
            self._all_tasks = self.repository.list_board_tasks(
                self.actor,
                include_finished=True,
                limit=10000,
            )
            projects = self.repository.list_projects(limit=10000)
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            return
        statuses = set(VIEW_STATUSES[self.view_mode])
        visible_ids = {
            task.project_id for task in self._all_tasks if task.status in statuses
        }
        self.projects = [
            project for project in projects if project.id in visible_ids
        ]
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
                str(project.urgent_count),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, project.id)
                self.project_table.setItem(row, column, item)
        if self.projects:
            self.project_table.selectRow(0)
        else:
            self.tasks = []
            self.task_table.setRowCount(0)
            self.reference_list.clear()
            self.detail_label.setText("当前状态下没有任务")
        self._update_actions()

    def _current_project(self) -> ProjectRecord | None:
        row = self.project_table.currentRow()
        if 0 <= row < len(self.projects):
            return self.projects[row]
        return None

    def _project_selected(self) -> None:
        project = self._current_project()
        if project is None:
            return
        self.detail_label.setText(
            f"{project.project_code} · 货号 {project.product_code or project.name}　"
            f"日期：{project.source_date or '未设置'}\n"
            f"共享目录：{project.project_relative_root}\n"
            f"备注：{project.note or '无'}"
        )
        statuses = set(VIEW_STATUSES[self.view_mode])
        self.tasks = [
            task
            for task in self._all_tasks
            if task.project_id == project.id and task.status in statuses
        ]
        self.task_table.setRowCount(0)
        for task in self.tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            values = (
                task.task_code,
                task.model_name,
                str(len(task.garment_assets)),
                task.assignee_name or "未分配",
                TASK_STATUS_LABELS.get(task.status, task.status),
                "是" if task.is_urgent else "否",
                utc_naive_to_local(
                    task.submitted_at or task.returned_at or task.created_at
                ).strftime("%Y-%m-%d %H:%M"),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, task.id)
                self.task_table.setItem(row, column, item)
        if self.tasks:
            self.task_table.selectRow(0)
        self._task_selected()

    def _task_selected(self) -> None:
        task = self._current_task()
        self.reference_list.clear()
        if task is not None:
            for asset in task.garment_assets:
                self._add_reference(task, asset.relative_path, asset.original_name)
            self._add_reference(task, task.model_path, task.model_name)
        self._update_actions()

    def _add_reference(
        self,
        task: ProjectTask,
        relative_path: str,
        name: str,
    ) -> None:
        path = self.resolver.resolve_project_path(
            task.project_relative_root,
            relative_path,
        )
        item = QListWidgetItem(name)
        pixmap = load_scaled_pixmap(path, QSize(82, 82))
        if not pixmap.isNull():
            item.setIcon(QIcon(pixmap))
        self.reference_list.addItem(item)

    def _current_task(self) -> ProjectTask | None:
        row = self.task_table.currentRow()
        if 0 <= row < len(self.tasks):
            return self.tasks[row]
        return None

    def _can_manage(self, project: ProjectRecord | None) -> bool:
        return bool(
            project
            and (
                self.actor.role == ROLE_ADMIN
                or project.owner_id == self.actor.id
            )
        )

    def _update_actions(self) -> None:
        project = self._current_project()
        task = self._current_task()
        can_manage = self._can_manage(project)
        editable = bool(
            task
            and can_manage
            and task.status not in (
                TASK_AWAITING_ACCEPTANCE,
                TASK_APPROVED,
                "cancelled",
            )
        )
        self.edit_sources.setEnabled(editable)
        self.urgent_button.setEnabled(editable)
        self.urgent_button.setText(
            "取消加急" if task and task.is_urgent else "设置加急"
        )
        self.view_results.setEnabled(bool(task))
        reviewable = bool(
            task
            and can_manage
            and task.status == TASK_AWAITING_ACCEPTANCE
        )
        self.return_button.setVisible(self.view_mode == "pending")
        self.approve_button.setVisible(self.view_mode == "pending")
        self.return_button.setEnabled(reviewable)
        self.approve_button.setEnabled(reviewable)

    def _edit_sources(self) -> None:
        task = self._current_task()
        project = self._current_project()
        if task is None or project is None:
            return
        draft = self.repository.get_project_draft(project.id)
        if draft is None:
            return
        dialog = SubtaskSourceDialog(
            self.repository,
            self.resolver,
            self.actor,
            task,
            draft.assets,
            self,
        )
        if dialog.exec():
            self.refresh()

    def _toggle_urgent(self) -> None:
        task = self._current_task()
        if task is None:
            return
        try:
            self.repository.set_subtask_urgent(
                task.id,
                self.actor,
                not task.is_urgent,
            )
        except Exception as exc:
            QMessageBox.warning(self, "操作失败", str(exc))
            return
        self.refresh()

    def _show_results(self) -> None:
        task = self._current_task()
        if task is None:
            return
        ResultGalleryDialog(
            self.resolver,
            task,
            self.repository.list_results(task.id),
            self,
        ).exec()

    def _approve_task(self) -> None:
        task = self._current_task()
        if task is None:
            return
        answer = QMessageBox.question(
            self,
            "验收通过",
            f"确认子任务 {task.task_code} 已通过吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.review_task(task.id, self.actor, approved=True)
        except Exception as exc:
            QMessageBox.warning(self, "验收失败", str(exc))
            return
        self.refresh()

    def _return_task(self) -> None:
        task = self._current_task()
        if task is None:
            return
        reason, ok = QInputDialog.getMultiLineText(
            self,
            "退回返工",
            "请填写需要修改的具体内容：",
        )
        if not ok:
            return
        if not reason.strip():
            QMessageBox.warning(self, "需要修改点", "退回时必须填写修改内容。")
            return
        try:
            self.repository.review_task(
                task.id,
                self.actor,
                approved=False,
                reason=reason,
            )
        except Exception as exc:
            QMessageBox.warning(self, "退回失败", str(exc))
            return
        self.refresh()
