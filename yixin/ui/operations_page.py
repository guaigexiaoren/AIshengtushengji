from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QInputDialog,
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
    PRIORITY_LABELS,
    PROJECT_STATUS_LABELS,
    ROLE_ADMIN,
    ROLE_OPERATIONS,
    TASK_AVAILABLE,
    TASK_AWAITING_ACCEPTANCE,
    TASK_STATUS_LABELS,
    AuthUser,
    ProjectRecord,
    ProjectTask,
)
from ..time_utils import utc_naive_to_local
from .project_editor import ProjectEditorDialog
from .result_gallery import ResultGalleryDialog


class OperationsPage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.projects: list[ProjectRecord] = []
        self.tasks: list[ProjectTask] = []
        self._build()
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("运营项目")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel("提交项目、查看全部进度，并对本人项目完成最终验收。")
        subtitle.setStyleSheet("color:#6d7a91;")
        title_box = QVBoxLayout()
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh)
        self.new_button = QPushButton("新建项目")
        self.new_button.setObjectName("PrimaryButton")
        self.new_button.clicked.connect(self._create_project)
        header.addWidget(refresh_button)
        if self.actor.role in (ROLE_OPERATIONS, ROLE_ADMIN):
            header.addWidget(self.new_button)
        outer.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.project_table = QTableWidget(0, 8)
        self.project_table.setHorizontalHeaderLabels(
            [
                "项目编号",
                "项目名称",
                "运营",
                "状态",
                "优先级",
                "截止时间",
                "任务进度",
                "待验收",
            ]
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
        detail_layout.setContentsMargins(0, 10, 0, 0)
        self.detail_label = QLabel("选择一个项目查看子任务")
        self.detail_label.setWordWrap(True)
        detail_layout.addWidget(self.detail_label)
        self.task_table = QTableWidget(0, 7)
        self.task_table.setHorizontalHeaderLabels(
            ["任务编号", "服装图", "模特图", "制作人员", "状态", "最终图片", "更新时间"]
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
        actions = QHBoxLayout()
        self.edit_button = QPushButton("修改项目")
        self.cancel_button = QPushButton("撤回项目")
        self.view_button = QPushButton("查看/对比成品")
        self.return_button = QPushButton("退回返工")
        self.approve_button = QPushButton("验收通过")
        self.approve_button.setObjectName("PrimaryButton")
        self.edit_button.clicked.connect(self._edit_project)
        self.cancel_button.clicked.connect(self._cancel_project)
        self.view_button.clicked.connect(self._view_results)
        self.return_button.clicked.connect(self._return_task)
        self.approve_button.clicked.connect(self._approve_task)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions.addWidget(self.view_button)
        actions.addWidget(self.return_button)
        actions.addWidget(self.approve_button)
        detail_layout.addLayout(actions)
        splitter.addWidget(detail)
        splitter.setSizes([360, 360])
        outer.addWidget(splitter, 1)
        self._update_actions()

    def refresh(self) -> None:
        try:
            shared_root = self.repository.get_shared_setting("share_root")
            if shared_root:
                self.resolver = SharedPathResolver(shared_root)
            self.projects = self.repository.list_projects()
        except Exception as exc:
            QMessageBox.warning(self, "读取项目失败", str(exc))
            return
        self.project_table.setRowCount(0)
        for project in self.projects:
            row = self.project_table.rowCount()
            self.project_table.insertRow(row)
            values = (
                project.project_code,
                project.name,
                project.owner_name,
                PROJECT_STATUS_LABELS.get(project.status, project.status),
                PRIORITY_LABELS.get(project.priority, project.priority),
                utc_naive_to_local(project.deadline).strftime("%Y-%m-%d %H:%M"),
                f"{project.approved_count}/{project.task_count}",
                str(project.awaiting_count),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, project.id)
                self.project_table.setItem(row, column, item)
        if self.projects:
            self.project_table.selectRow(0)
        else:
            self.detail_label.setText("暂无项目")
            self.task_table.setRowCount(0)

    def _current_project(self) -> ProjectRecord | None:
        row = self.project_table.currentRow()
        if not 0 <= row < len(self.projects):
            return None
        return self.projects[row]

    def _project_selected(self) -> None:
        project = self._current_project()
        if project is None:
            return
        self.detail_label.setText(
            f"{project.project_code} · {project.name}　"
            f"品牌/平台：{project.brand_platform or '未填写'}　"
            f"品类：{project.category or '未填写'}\n"
            f"备注：{project.note or '无'}"
        )
        try:
            self.tasks = self.repository.list_project_tasks(project.id)
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            self.tasks = []
        self.task_table.setRowCount(0)
        for task in self.tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            values = (
                task.task_code,
                task.garment_name,
                task.model_name,
                task.assignee_name or "未分配",
                TASK_STATUS_LABELS.get(task.status, task.status),
                str(task.final_quantity),
                utc_naive_to_local(task.submitted_at).strftime("%Y-%m-%d %H:%M")
                if task.submitted_at
                else "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, task.id)
                self.task_table.setItem(row, column, item)
        if self.tasks:
            self.task_table.selectRow(0)
        self._update_actions()

    def _current_task(self) -> ProjectTask | None:
        row = self.task_table.currentRow()
        if not 0 <= row < len(self.tasks):
            return None
        return self.tasks[row]

    def _can_manage_project(self, project: ProjectRecord) -> bool:
        return (
            self.actor.role == ROLE_ADMIN or project.owner_id == self.actor.id
        )

    def _task_selected(self) -> None:
        self._update_actions()

    def _update_actions(self) -> None:
        project = self._current_project()
        task = self._current_task()
        can_manage = bool(project and self._can_manage_project(project))
        all_available = bool(self.tasks) and all(
            task.status == TASK_AVAILABLE for task in self.tasks
        )
        can_cancel = bool(
            can_manage
            and project
            and project.status not in ("completed", "cancelled")
            and (self.actor.role == ROLE_ADMIN or all_available)
        )
        self.edit_button.setEnabled(
            bool(can_cancel and all_available)
        )
        self.cancel_button.setEnabled(can_cancel)
        self.view_button.setEnabled(bool(task))
        can_review = bool(
            can_manage and task and task.status == TASK_AWAITING_ACCEPTANCE
        )
        self.return_button.setEnabled(can_review)
        self.approve_button.setEnabled(can_review)

    def _create_project(self) -> None:
        shared_root = self.repository.get_shared_setting("share_root")
        if not shared_root:
            QMessageBox.warning(
                self,
                "未配置共享盘",
                "请先让管理员在管理端设置统一 UNC 共享盘根目录。",
            )
            return
        resolver = SharedPathResolver(shared_root)
        dialog = ProjectEditorDialog(
            self.repository,
            resolver,
            self.actor,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _edit_project(self) -> None:
        project = self._current_project()
        if project is None:
            return
        draft = self.repository.get_project_draft(project.id)
        if draft is None:
            QMessageBox.warning(self, "无法修改", "项目不存在")
            return
        shared_root = self.repository.get_shared_setting("share_root")
        resolver = SharedPathResolver(shared_root)
        dialog = ProjectEditorDialog(
            self.repository,
            resolver,
            self.actor,
            existing=draft,
            project_id=project.id,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _cancel_project(self) -> None:
        project = self._current_project()
        if project is None:
            return
        answer = QMessageBox.question(
            self,
            "撤回项目",
            f"确定撤回项目“{project.project_code}”吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.cancel_project(project.id, self.actor)
        except Exception as exc:
            QMessageBox.warning(self, "撤回失败", str(exc))
            return
        self.refresh()

    def _view_results(self) -> None:
        task = self._current_task()
        if task is None:
            return
        results = self.repository.list_results(task.id)
        ResultGalleryDialog(self.resolver, task, results, self).exec()

    def _approve_task(self) -> None:
        task = self._current_task()
        if task is None:
            return
        answer = QMessageBox.question(
            self,
            "验收通过",
            f"确认任务 {task.task_code} 的所有最终图片已符合要求吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.review_task(
                task.id,
                self.actor,
                approved=True,
            )
        except Exception as exc:
            QMessageBox.warning(self, "验收失败", str(exc))
            return
        self._project_selected()

    def _return_task(self) -> None:
        task = self._current_task()
        if task is None:
            return
        reason, ok = QInputDialog.getMultiLineText(
            self,
            "退回返工",
            "请填写具体修改要求：",
        )
        if not ok:
            return
        if not reason.strip():
            QMessageBox.warning(self, "需要原因", "退回任务必须填写原因。")
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
        self._project_selected()
