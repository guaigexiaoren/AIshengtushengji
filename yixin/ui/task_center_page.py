from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..api import NanoBananaClient
from ..collaboration.local_sources import LocalSourceState, LocalTaskSourceStore
from ..collaboration.manual_upload import upload_manual_candidates
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    ATTEMPT_FAILED,
    ATTEMPT_QUEUED,
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    ROLE_ADMIN,
    ROLE_AI,
    TASK_AVAILABLE,
    TASK_APPROVED,
    TASK_AWAITING_ACCEPTANCE,
    TASK_CANCELLED,
    TASK_CLAIMED,
    TASK_RETURNED,
    TASK_SKIPPED,
    TASK_STATUS_LABELS,
    TASK_WORKING,
    AuthUser,
    ProjectTask,
)
from ..collaboration.worker import ProjectGenerationWorker
from ..constants import (
    ASPECT_RATIOS,
    BUILTIN_PROMPT_TEMPLATES,
    MODEL_LABELS,
    MODEL_SELECTION_OPTIONS,
    RESOLUTIONS,
)
from ..library import LibraryService
from ..model_router import choose_model
from ..local_export import export_original_image
from ..settings import (
    AUTO_STRATEGY_QUALITY,
    LOCAL_SAVE_FIXED,
    LOCAL_SAVE_REFERENCE,
    AppSettings,
)
from ..storage import FileStore
from ..time_utils import utc_naive_to_local
from .crop import CropDialog
from .date_filter import OptionalDateEdit
from .dialogs import CompareDialog, LibraryPickerDialog
from .result_gallery import ResultGallery
from .save_helpers import show_saved_toast
from .widgets import ImageStrip, scan_image_inputs


ATTEMPT_LABELS = {
    ATTEMPT_QUEUED: "排队中",
    ATTEMPT_RUNNING: "生成中",
    ATTEMPT_SUCCEEDED: "成功",
    ATTEMPT_FAILED: "失败",
    "manual_ps": "PS成品",
}


@dataclass(frozen=True)
class ProjectTaskGroup:
    project_id: int
    project_code: str
    product_code: str
    project_scope: str
    project_name: str
    tasks: tuple[ProjectTask, ...]
    created_at: object
    status: str


class DroppableImageStrip(ImageStrip):
    files_dropped = Signal(list)

    @staticmethod
    def _local_paths(event) -> list[Path]:
        if not event.mimeData().hasUrls():
            return []
        return [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile()
        ]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._local_paths(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self._local_paths(event)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class TaskCenterPage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        client: NanoBananaClient,
        key_store,
        file_store: FileStore,
        app_settings: AppSettings,
        library_service: LibraryService | None = None,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.client = client
        self.key_store = key_store
        self.file_store = file_store
        self.app_settings = app_settings
        self.library_service = library_service
        self.tasks: list[ProjectTask] = []
        self.project_groups: list[ProjectTaskGroup] = []
        self.visible_project_tasks: list[ProjectTask] = []
        self.current_project_id: int | None = None
        self.current_task: ProjectTask | None = None
        self._groups_by_id: dict[int, ProjectTaskGroup] = {}
        self._tasks_by_id: dict[int, ProjectTask] = {}
        self.local_store: LocalTaskSourceStore | None = None
        self.local_state: LocalSourceState | None = None
        self.active_workers: set[ProjectGenerationWorker] = set()
        self.waiting_jobs: deque[dict] = deque()
        self._build()
        self.queue_timer = QTimer(self)
        self.queue_timer.setInterval(1200)
        self.queue_timer.timeout.connect(self._refresh_queue)
        self.queue_timer.start()
        if auto_load:
            self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("任务中心")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel("左侧选任务并查看队列，中间写提示词，右侧管理候选图。")
        subtitle.setStyleSheet("color:#6d7a91;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.include_finished = QCheckBox("显示已完成")
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        header.addWidget(self.include_finished)
        header.addWidget(refresh)
        outer.addLayout(header)

        filters = QHBoxLayout()
        self.project_filter_combo = QComboBox()
        self.project_filter_combo.addItem("全部任务", None)
        self.product_search = QLineEdit()
        self.product_search.setPlaceholderText("搜索货号")
        self.center_start_date = self._optional_date_edit("开始日期")
        self.center_end_date = self._optional_date_edit("结束日期")
        reset_filter = QPushButton("重置筛选")
        filters.addWidget(QLabel("任务"))
        filters.addWidget(self.project_filter_combo, 1)
        filters.addWidget(self.product_search, 1)
        filters.addWidget(QLabel("创建时间"))
        filters.addWidget(self.center_start_date)
        filters.addWidget(QLabel("至"))
        filters.addWidget(self.center_end_date)
        filters.addWidget(reset_filter)
        outer.addLayout(filters)
        self.project_filter_combo.currentIndexChanged.connect(
            self._apply_task_filters
        )
        self.product_search.textChanged.connect(self._apply_task_filters)
        self.center_start_date.dateChanged.connect(self._apply_task_filters)
        self.center_end_date.dateChanged.connect(self._apply_task_filters)
        reset_filter.clicked.connect(self._reset_filters)
        self.include_finished.toggled.connect(self._apply_task_filters)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_task_panel())
        splitter.addWidget(self._build_generation_panel())
        splitter.addWidget(self._build_candidate_panel())
        splitter.setSizes([480, 580, 440])
        outer.addWidget(splitter, 1)

    @staticmethod
    def _optional_date_edit(placeholder: str) -> OptionalDateEdit:
        return OptionalDateEdit(placeholder)

    def _build_task_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        self.task_splitter = QSplitter(Qt.Orientation.Vertical)
        project_host = QWidget()
        project_layout = QVBoxLayout(project_host)
        project_layout.setContentsMargins(0, 0, 0, 0)
        project_title = QLabel("任务列表")
        project_title.setStyleSheet("font-size:16px;font-weight:700;")
        project_layout.addWidget(project_title)
        self.project_table = QTableWidget(0, 5)
        self.project_table.setHorizontalHeaderLabels(
            ["任务编码", "货号", "子任务进度", "状态", "创建时间"]
        )
        self.project_table.verticalHeader().setVisible(False)
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
        for column, width in enumerate((195, 70, 90, 80, 125)):
            self.project_table.setColumnWidth(column, width)
        self.project_table.setSortingEnabled(True)
        self.project_table.itemSelectionChanged.connect(
            self._project_selected
        )
        project_layout.addWidget(self.project_table, 1)
        self.task_splitter.addWidget(project_host)

        subtask_host = QWidget()
        subtask_layout = QVBoxLayout(subtask_host)
        subtask_layout.setContentsMargins(0, 0, 0, 0)
        subtask_title = QLabel("子任务明细")
        subtask_title.setStyleSheet("font-size:16px;font-weight:700;")
        subtask_layout.addWidget(subtask_title)
        self.task_table = QTableWidget(0, 5)
        self.task_table.setHorizontalHeaderLabels(
            ["子任务编码", "制作人员", "状态", "加急", "创建时间"]
        )
        self.task_table.verticalHeader().setVisible(False)
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
        for column, width in enumerate((195, 105, 80, 125, 50)):
            self.task_table.setColumnWidth(column, width)
        self.task_table.setSortingEnabled(True)
        self.task_table.itemSelectionChanged.connect(self._task_selected)
        subtask_layout.addWidget(self.task_table, 1)
        self.task_splitter.addWidget(subtask_host)
        self.task_splitter.setSizes([420, 400])
        layout.addWidget(self.task_splitter, 1)
        return panel

    def _build_generation_panel(self) -> QWidget:
        self.generation_splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(8, 0, 8, 0)
        source_tools = QHBoxLayout()
        source_tools.setSpacing(4)
        info_title = QLabel("子任务信息与参考图")
        info_title.setStyleSheet("font-size:16px;font-weight:700;")
        source_tools.addWidget(info_title)
        source_tools.addStretch(1)
        self.add_source_files_button = QPushButton("添加图片")
        self.add_source_folder_button = QPushButton("添加文件夹")
        self.add_source_library_button = QPushButton("从作品库添加")
        self.crop_source_button = QPushButton("裁剪")
        self.restore_source_button = QPushButton("恢复")
        self.remove_source_button = QPushButton("移除所选")
        self.clear_sources_button = QPushButton("清空")
        for button in (
            self.add_source_files_button,
            self.add_source_folder_button,
            self.add_source_library_button,
            self.crop_source_button,
            self.restore_source_button,
            self.remove_source_button,
            self.clear_sources_button,
        ):
            button.setStyleSheet("padding:5px 7px;")
            source_tools.addWidget(button)
        top_layout.addLayout(source_tools)
        self.task_detail = QLabel("选择一个子任务")
        self.task_detail.setWordWrap(False)
        self.task_detail.setStyleSheet("color:#60708a;")
        self.task_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        top_layout.addWidget(self.task_detail)
        self.local_source_hint = QLabel()
        self.local_source_hint.hide()
        top_layout.addWidget(self.local_source_hint)
        self.source_strip = DroppableImageStrip(icon_size=82)
        self.source_strip.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.source_strip.files_dropped.connect(
            self._handle_source_drop
        )
        top_layout.addWidget(self.source_strip)
        self.add_source_files_button.clicked.connect(
            self._add_source_files
        )
        self.add_source_folder_button.clicked.connect(
            self._add_source_folder
        )
        self.add_source_library_button.clicked.connect(
            self._add_source_library
        )
        self.crop_source_button.clicked.connect(self._crop_source)
        self.restore_source_button.clicked.connect(self._restore_source)
        self.remove_source_button.clicked.connect(self._remove_sources)
        self.clear_sources_button.clicked.connect(self._clear_sources)
        self.generation_splitter.addWidget(top)

        middle = QWidget()
        layout = QVBoxLayout(middle)
        layout.setContentsMargins(8, 0, 8, 0)
        prompt_card = QGroupBox("提示词与参数")
        prompt_layout = QVBoxLayout(prompt_card)
        template_row = QHBoxLayout()
        self.template_combo = QComboBox()
        self.template_combo.addItem("选择提示词模板", None)
        self.template_combo.currentIndexChanged.connect(self._apply_template)
        save_template = QPushButton("保存个人模板")
        save_template.clicked.connect(self._save_template)
        template_row.addWidget(self.template_combo, 1)
        template_row.addWidget(save_template)
        prompt_layout.addLayout(template_row)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText(
            "描述服装替换要求；衣服图按顺序排在前，最后一张为模特图。"
        )
        self.prompt_edit.setMinimumHeight(120)
        prompt_layout.addWidget(self.prompt_edit)
        form = QGridLayout()
        self.model_combo = QComboBox()
        for label, model_id in MODEL_SELECTION_OPTIONS:
            self.model_combo.addItem(label, model_id)
        self.aspect_combo = QComboBox()
        for value in ASPECT_RATIOS:
            self.aspect_combo.addItem(value, value)
        self.aspect_combo.setCurrentText("3:4")
        self.resolution_combo = QComboBox()
        for value in RESOLUTIONS:
            self.resolution_combo.addItem(
                "自动（不发送）" if value == "auto" else value,
                value,
            )
        self.resolution_combo.setCurrentText("2K")
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 4)
        self.count_spin.setValue(1)
        self.count_spin.hide()
        self.parameters_grid = form
        model_group = QWidget()
        model_layout = QVBoxLayout(model_group)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.addWidget(QLabel("模型"))
        model_layout.addWidget(self.model_combo)
        aspect_group = QWidget()
        aspect_layout = QVBoxLayout(aspect_group)
        aspect_layout.setContentsMargins(0, 0, 0, 0)
        aspect_layout.addWidget(QLabel("宽高比"))
        aspect_layout.addWidget(self.aspect_combo)
        resolution_group = QWidget()
        resolution_layout = QVBoxLayout(resolution_group)
        resolution_layout.setContentsMargins(0, 0, 0, 0)
        resolution_layout.addWidget(QLabel("分辨率"))
        resolution_layout.addWidget(self.resolution_combo)
        self.generate_button = QPushButton("加入生成队列")
        self.generate_button.setObjectName("PrimaryButton")
        self.generate_button.clicked.connect(self._enqueue_generation)
        form.addWidget(model_group, 0, 0)
        form.addWidget(aspect_group, 0, 1)
        form.addWidget(resolution_group, 0, 2)
        form.addWidget(self.generate_button, 0, 3)
        for column in range(4):
            form.setColumnStretch(column, 1)
        prompt_layout.addLayout(form)
        layout.addWidget(prompt_card)
        self.generation_splitter.addWidget(middle)

        queue_card = QGroupBox("生成任务队列")
        queue_layout = QVBoxLayout(queue_card)
        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setHorizontalHeaderLabels(
            ["轮次", "操作人员", "状态", "创建时间", "结果数"]
        )
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.queue_table.horizontalHeader().setStretchLastSection(True)
        self.queue_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((70, 120, 110, 145, 70)):
            self.queue_table.setColumnWidth(column, width)
        queue_layout.addWidget(self.queue_table)
        self.queue_hint = QLabel()
        self.queue_hint.setStyleSheet("color:#60708a;")
        queue_layout.addWidget(self.queue_hint)
        self.generation_splitter.addWidget(queue_card)
        self.generation_splitter.setSizes([220, 380, 260])
        return self.generation_splitter

    def _build_candidate_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        self.result_title = QLabel("当前任务生成结果")
        self.result_title.setStyleSheet("font-size:16px;font-weight:700;")
        layout.addWidget(self.result_title)
        toolbar = QHBoxLayout()
        self.selection_label = QLabel("已选择 0 张最终图")
        toolbar.addWidget(self.selection_label, 1)
        layout.addLayout(toolbar)
        self.gallery = ResultGallery(self.resolver, checkable=True)
        self.gallery.list_widget.itemChanged.connect(self._selection_changed)
        layout.addWidget(self.gallery, 1)
        button_row = QHBoxLayout()
        self.retry_button = QPushButton("重试任务")
        self.save_button = QPushButton("保存图片")
        self.upload_button = QPushButton("上传修后图")
        self.compare_button = QPushButton("查看/对比成品")
        self.submit_button = QPushButton("提交验收")
        self.skip_button = QPushButton("剩余不制作")
        self.submit_button.setObjectName("PrimaryButton")
        self.retry_button.clicked.connect(self._retry_task)
        self.save_button.clicked.connect(self._save_result)
        self.upload_button.clicked.connect(self._upload_manual)
        self.compare_button.clicked.connect(self._compare_result)
        self.submit_button.clicked.connect(self._submit)
        self.skip_button.clicked.connect(self._skip_remaining)
        for button in (
            self.retry_button,
            self.save_button,
            self.upload_button,
            self.compare_button,
            self.submit_button,
            self.skip_button,
        ):
            button.setMinimumHeight(36)
            button_row.addWidget(button)
        layout.addLayout(button_row)
        return panel

    def refresh(self) -> None:
        try:
            shared_root = self.repository.get_shared_setting("share_root")
            if shared_root:
                self.resolver = SharedPathResolver(shared_root)
            self.tasks = self.repository.list_board_tasks(
                self.actor,
                include_finished=True,
                limit=10000,
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            return
        self._refresh_templates()
        self._build_project_groups()
        self._populate_project_filter()
        self._apply_task_filters()
        self._resume_queued_jobs()
        self._pump_queue()
        self._update_controls()

    @staticmethod
    def _group_status(tasks: tuple[ProjectTask, ...]) -> str:
        statuses = {task.status for task in tasks}
        if (
            TASK_APPROVED in statuses
            and statuses <= {TASK_APPROVED, TASK_SKIPPED}
        ):
            return "已完成"
        if TASK_AWAITING_ACCEPTANCE in statuses:
            return "待审核"
        if statuses <= {TASK_AVAILABLE}:
            return "已提交"
        if statuses <= {TASK_CANCELLED}:
            return "已取消"
        return "制作中"

    def _build_project_groups(self) -> None:
        grouped: dict[int, list[ProjectTask]] = {}
        for task in self.tasks:
            grouped.setdefault(task.project_id, []).append(task)
        self.project_groups = [
            ProjectTaskGroup(
                project_id=project_id,
                project_code=project_tasks[0].project_code,
                product_code=(
                    project_tasks[0].product_code
                    or project_tasks[0].project_name
                ),
                project_scope=project_tasks[0].project_scope,
                project_name=project_tasks[0].access_project_name,
                tasks=tuple(project_tasks),
                created_at=min(task.created_at for task in project_tasks),
                status=self._group_status(tuple(project_tasks)),
            )
            for project_id, project_tasks in grouped.items()
        ]
        self.project_groups.sort(
            key=lambda group: group.created_at,
            reverse=True,
        )
        self._groups_by_id = {
            group.project_id: group for group in self.project_groups
        }

    def _populate_project_filter(self) -> None:
        selected = self.project_filter_combo.currentData()
        self.project_filter_combo.blockSignals(True)
        self.project_filter_combo.clear()
        self.project_filter_combo.addItem("全部任务", None)
        seen: set[tuple[str, str]] = set()
        for group in self.project_groups:
            key = (
                group.project_scope,
                group.project_name or group.product_code,
            )
            if key in seen:
                continue
            seen.add(key)
            self.project_filter_combo.addItem(
                key[1],
                key,
            )
        index = self.project_filter_combo.findData(selected)
        self.project_filter_combo.setCurrentIndex(max(0, index))
        self.project_filter_combo.blockSignals(False)

    def _reset_filters(self) -> None:
        self.project_filter_combo.setCurrentIndex(0)
        self.product_search.clear()
        self.center_start_date.clear()
        self.center_end_date.clear()
        self._apply_task_filters()

    def _apply_task_filters(self, *_args) -> None:
        keyword = self.product_search.text().strip().casefold()
        project_key = self.project_filter_combo.currentData()
        start = self.center_start_date.python_date()
        end = self.center_end_date.python_date()
        include_finished = self.include_finished.isChecked()

        def task_visible(task: ProjectTask) -> bool:
            return include_finished or task.status not in (
                TASK_APPROVED,
                TASK_SKIPPED,
                TASK_CANCELLED,
            )

        visible_groups = []
        for group in self.project_groups:
            created = utc_naive_to_local(group.created_at).date()
            group_key = (
                group.project_scope,
                group.project_name or group.product_code,
            )
            if project_key is not None and group_key != project_key:
                continue
            if keyword and keyword not in group.product_code.casefold():
                continue
            if start is not None and created < start:
                continue
            if end is not None and created > end:
                continue
            if not any(task_visible(task) for task in group.tasks):
                continue
            visible_groups.append(group)
        self.project_table.setSortingEnabled(False)
        self.project_table.setRowCount(0)
        for group in visible_groups:
            row = self.project_table.rowCount()
            self.project_table.insertRow(row)
            approved = sum(
                task.status == TASK_APPROVED for task in group.tasks
            )
            skipped = sum(
                task.status == TASK_SKIPPED for task in group.tasks
            )
            progress = f"{approved}/{len(group.tasks)}"
            if skipped:
                progress += f" · 无需制作 {skipped}"
            values = (
                group.project_code,
                group.product_code,
                progress,
                group.status,
                utc_naive_to_local(group.created_at).strftime(
                    "%Y-%m-%d %H:%M"
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        group.project_id,
                    )
                self.project_table.setItem(row, column, item)
        self.project_table.setSortingEnabled(True)
        if not self.project_table.rowCount():
            self.current_project_id = None
            self.current_task = None
            self.task_table.setRowCount(0)
            self.source_strip.clear()
            self.gallery.clear()
            return
        target_row = 0
        for row in range(self.project_table.rowCount()):
            item = self.project_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == (
                self.current_project_id
            ):
                target_row = row
                break
        self.project_table.selectRow(target_row)
        self._project_selected()

    def _project_selected(self) -> None:
        row = self.project_table.currentRow()
        item = self.project_table.item(row, 0) if row >= 0 else None
        project_id = (
            int(item.data(Qt.ItemDataRole.UserRole))
            if item is not None
            else None
        )
        group = self._groups_by_id.get(project_id)
        if group is None:
            return
        self.current_project_id = group.project_id
        include_finished = self.include_finished.isChecked()
        self.visible_project_tasks = [
            task
            for task in group.tasks
            if include_finished
            or task.status not in (
                TASK_APPROVED,
                TASK_SKIPPED,
                TASK_CANCELLED,
            )
        ]
        self._tasks_by_id = {
            task.id: task for task in self.visible_project_tasks
        }
        selected_task_id = self.current_task.id if self.current_task else None
        self.task_table.setSortingEnabled(False)
        self.task_table.setRowCount(0)
        for task in self.visible_project_tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            values = (
                task.task_code,
                task.assignee_name or "未分配",
                TASK_STATUS_LABELS.get(task.status, task.status),
                "是" if group.tasks[0].project_is_urgent else "否",
                utc_naive_to_local(task.created_at).strftime(
                    "%Y-%m-%d %H:%M"
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, task.id)
                if group.tasks[0].project_is_urgent:
                    item.setBackground(Qt.GlobalColor.yellow)
                self.task_table.setItem(row, column, item)
        self.task_table.setSortingEnabled(True)
        if not self.task_table.rowCount():
            self.current_task = None
            self.source_strip.clear()
            self.gallery.clear()
            self.task_detail.setText("该任务当前没有可显示的子任务")
            self._update_controls()
            return
        target_row = 0
        for row in range(self.task_table.rowCount()):
            item = self.task_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == (
                selected_task_id
            ):
                target_row = row
                break
        self.task_table.selectRow(target_row)
        self._task_selected()

    def _task_selected(self) -> None:
        row = self.task_table.currentRow()
        item = self.task_table.item(row, 0) if row >= 0 else None
        task_id = (
            int(item.data(Qt.ItemDataRole.UserRole))
            if item is not None
            else None
        )
        self.current_task = self._tasks_by_id.get(task_id)
        task = self.current_task
        self.source_strip.clear()
        if task is None:
            self.local_store = None
            self.local_state = None
            self.gallery.clear()
            self._update_controls()
            return
        try:
            self.local_store = LocalTaskSourceStore(
                self.file_store.paths.root / "local_sources",
                self.actor.id,
                task,
                self.resolver,
            )
            self.local_state = self.local_store.state
        except Exception as exc:
            self.local_store = None
            self.local_state = None
            self.local_source_hint.setText(f"本机素材加载失败：{exc}")
        self._refresh_local_sources(select_model=True)
        prompt = task.prompt_text.strip()
        if not prompt:
            prompt = BUILTIN_PROMPT_TEMPLATES[0][1]
        self.template_combo.blockSignals(True)
        self.template_combo.setCurrentIndex(0)
        self.template_combo.blockSignals(False)
        self.prompt_edit.setPlainText(prompt)
        if task.model in MODEL_LABELS:
            self.model_combo.setCurrentIndex(self.model_combo.findData(task.model))
        if task.aspect_ratio:
            self.aspect_combo.setCurrentText(task.aspect_ratio)
        if task.resolution:
            self.resolution_combo.setCurrentIndex(
                self.resolution_combo.findData(task.resolution)
            )
        self._load_results()
        self._refresh_queue()
        self._update_controls()

    def _refresh_local_sources(self, *, select_model: bool = False) -> None:
        state = self.local_state
        if state is None:
            self.source_strip.clear()
            return
        items = []
        for index, image in enumerate(state.garments):
            items.append(
                (
                    image.path,
                    f"衣服 {index + 1}",
                    ("garment", index),
                )
            )
        if state.model is not None:
            items.append((state.model.path, "模特", ("model", 0)))
        self.source_strip.set_images(items)
        if items:
            self.source_strip.setCurrentRow(
                len(items) - 1 if select_model else 0
            )
        self._update_local_source_hint()
        self._update_task_detail()

    def _update_task_detail(self) -> None:
        task = self.current_task
        if task is None:
            return
        garment_count = (
            len(self.local_state.garments)
            if self.local_state is not None
            else len(task.garment_assets)
        )
        self.task_detail.setText(
            f"{task.task_code} · 货号 "
            f"{task.product_code or task.project_name} · 制作 "
            f"{task.assignee_name or '未分配'} · "
            f"{TASK_STATUS_LABELS.get(task.status, task.status)} · "
            f"衣服 {garment_count} 张"
        )

    def _update_local_source_hint(self) -> None:
        state = self.local_state
        if state is None:
            self.local_source_hint.setText("尚未加载本机参考素材")
            return
        if not state.garments or state.model is None:
            self.local_source_hint.setText(
                "本机素材不完整：至少需要一张衣服图和一张模特图"
            )
            return
        shared_garments = tuple(
            asset.content_hash for asset in self.current_task.garment_assets
        ) if self.current_task else ()
        local_garments = tuple(
            image.content_hash for image in state.garments
        )
        customized = (
            local_garments != shared_garments
            or (
                self.current_task is not None
                and state.model.content_hash != self.current_task.model_hash
            )
        )
        self.local_source_hint.setText(
            "本机素材已修改，仅影响当前账号和本机生成"
            if customized
            else "当前使用共享盘素材；本机修改仅影响当前账号和本机生成"
        )

    def _is_assignee(self, task: ProjectTask | None) -> bool:
        return bool(task and task.assignee_id == self.actor.id)

    def _can_edit_local_sources(self) -> bool:
        task = self.current_task
        return bool(
            task
            and self.local_store is not None
            and task.status not in (
                "awaiting_acceptance",
                "approved",
                "cancelled",
            )
            and (
                self.actor.role == ROLE_ADMIN
                or self._is_assignee(task)
            )
        )

    def _current_source_position(self) -> tuple[str, int] | None:
        item = self.source_strip.currentItem()
        position = (
            item.data(Qt.ItemDataRole.UserRole)
            if item is not None
            else None
        )
        return position if isinstance(position, tuple) else None

    def _selected_source_positions(self) -> list[tuple[str, int]]:
        return [
            position
            for item in self.source_strip.selectedItems()
            if isinstance(
                position := item.data(Qt.ItemDataRole.UserRole),
                tuple,
            )
        ]

    def _selected_source_role(self) -> str | None:
        position = self._current_source_position()
        if position is None:
            QMessageBox.information(
                self,
                "请选择参考图",
                "请先选择一张“衣服”或“模特”缩略图，新图片会加入同一类型。",
            )
            return None
        return position[0]

    def _add_source_files(self) -> None:
        if not self._can_edit_local_sources():
            return
        role = self._selected_source_role()
        if role is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择衣服图" if role == "garment" else "选择模特图",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        self._add_local_paths(role, [Path(path) for path in paths])

    def _add_source_folder(self) -> None:
        if not self._can_edit_local_sources():
            return
        role = self._selected_source_role()
        if role is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择参考图片文件夹",
        )
        if not folder:
            return
        paths, errors = scan_image_inputs([Path(folder)])
        if errors:
            QMessageBox.warning(
                self,
                "部分图片未导入",
                "\n".join(errors[:8]),
            )
        self._add_local_paths(
            role,
            paths,
        )

    def _handle_source_drop(self, raw_paths: list) -> None:
        if not self._can_edit_local_sources():
            return
        role = self._selected_source_role()
        if role is None:
            return
        paths, errors = scan_image_inputs(
            [Path(path) for path in raw_paths]
        )
        if errors:
            QMessageBox.warning(
                self,
                "部分图片未导入",
                "\n".join(errors[:8]),
            )
        self._add_local_paths(role, paths)

    def _add_source_library(self) -> None:
        if not self._can_edit_local_sources():
            return
        if self.library_service is None:
            QMessageBox.information(
                self,
                "作品库不可用",
                "当前环境没有加载作品库。",
            )
            return
        role = self._selected_source_role()
        if role is None:
            return
        current = (
            len(self.local_state.garments)
            if role == "garment"
            else (1 if self.local_state.model is not None else 0)
        )
        remaining = 13 - current if role == "garment" else 1
        if remaining <= 0:
            QMessageBox.information(
                self,
                "已达到上限",
                "衣服图最多 13 张，模特图最多 1 张。",
            )
            return
        dialog = LibraryPickerDialog(
            self.library_service.database,
            remaining,
            self,
        )
        if dialog.exec() == dialog.DialogCode.Accepted:
            self._add_local_paths(role, dialog.selected_paths())

    def _add_local_paths(self, role: str, paths: list[Path]) -> None:
        if not paths or self.local_store is None:
            return
        if role == "model":
            paths = paths[:1]
        try:
            self.local_state = self.local_store.add_paths(role, paths)
        except Exception as exc:
            QMessageBox.warning(self, "添加失败", str(exc))
            return
        self._refresh_local_sources(select_model=role == "model")
        self._update_controls()

    def _crop_source(self) -> None:
        if not self._can_edit_local_sources():
            return
        position = self._current_source_position()
        if position is None or self.local_store is None:
            return
        role, index = position
        image = self.local_store.image_at(role, index)
        if image is None:
            return
        dialog = CropDialog(image.path, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        crop_box = dialog.crop_box()
        if crop_box is None:
            return
        try:
            cropped = self.file_store.save_cropped_reference(
                image,
                crop_box,
            )
            self.local_state = self.local_store.replace_image(
                role,
                index,
                cropped,
            )
        except Exception as exc:
            QMessageBox.warning(self, "裁剪失败", str(exc))
            return
        self._refresh_local_sources()
        self._update_unstarted_queue_sources()
        self._update_controls()

    def _restore_source(self) -> None:
        if not self._can_edit_local_sources():
            return
        position = self._current_source_position()
        if self.local_store is None:
            return
        if (
            position is None
            and self.local_state is not None
            and self.local_state.references()
        ):
            QMessageBox.information(
                self,
                "请选择参考图",
                "单张恢复请先选择一张参考图；清空后可直接恢复全部初始素材。",
            )
            return
        try:
            self.local_state = (
                self.local_store.restore_all()
                if position is None
                else self.local_store.restore_image(*position)
            )
        except Exception as exc:
            QMessageBox.warning(self, "恢复失败", str(exc))
            return
        self._refresh_local_sources()
        self._update_controls()

    def _remove_sources(self) -> None:
        if not self._can_edit_local_sources() or self.local_store is None:
            return
        selected = self._selected_source_positions()
        if not selected:
            QMessageBox.information(
                self,
                "未选择图片",
                "请先选择要移除的参考图。",
            )
            return
        for role in ("garment", "model"):
            indexes = [
                index
                for item_role, index in selected
                if item_role == role
            ]
            if indexes:
                self.local_state = self.local_store.remove_images(
                    role,
                    indexes,
                )
        self._refresh_local_sources()
        self._update_controls()

    def _clear_sources(self) -> None:
        if not self._can_edit_local_sources() or self.local_store is None:
            return
        answer = QMessageBox.question(
            self,
            "清空本机素材",
            "确认清空当前任务的全部本机参考图吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.local_state = self.local_store.clear()
        self._refresh_local_sources()
        self._update_controls()

    def _update_unstarted_queue_sources(self) -> None:
        task = self.current_task
        if task is None or self.local_store is None:
            return
        try:
            attempts = self.repository.list_queued_attempts(self.actor.id)
        except Exception:
            attempts = []
        queued_ids = {
            attempt.id
            for attempt in attempts
            if attempt.task_id == task.id
        }
        for job in self.waiting_jobs:
            if job["task"].id == task.id:
                queued_ids.add(job["attempt"].id)
        for attempt_id in queued_ids:
            try:
                self.local_store.save_attempt_snapshot(attempt_id)
            except Exception:
                continue
        references = self.local_state.references() if self.local_state else ()
        for job in self.waiting_jobs:
            if job["task"].id == task.id:
                job["local_references"] = references

    def _update_controls(self) -> None:
        task = self.current_task
        task_busy = bool(
            task
            and (
                any(worker.task.id == task.id for worker in self.active_workers)
                or any(job["task"].id == task.id for job in self.waiting_jobs)
            )
        )
        can_claim = bool(
            task
            and self.actor.role in (ROLE_AI, ROLE_ADMIN)
            and (
                task.status == TASK_AVAILABLE
                or (
                    task.status == TASK_RETURNED
                    and (
                        self._is_assignee(task)
                        or self.actor.role == ROLE_ADMIN
                    )
                )
            )
        )
        can_work = bool(
            task
            and self.actor.role in (ROLE_AI, ROLE_ADMIN)
            and self._is_assignee(task)
            and task.status in (
                TASK_CLAIMED,
                TASK_WORKING,
                TASK_RETURNED,
            )
        )
        local_ready = bool(
            self.local_state
            and self.local_state.garments
            and self.local_state.model is not None
        )
        can_edit_sources = self._can_edit_local_sources()
        self.generate_button.setEnabled(
            (can_claim or can_work) and local_ready
        )
        self.upload_button.setEnabled(
            (can_claim or can_work) and not task_busy
        )
        self.submit_button.setEnabled(
            can_work and not task_busy and bool(self.gallery.selected_result_ids())
        )
        self.retry_button.setEnabled(can_work)
        self.save_button.setEnabled(self.gallery.current_path() is not None)
        self.compare_button.setEnabled(self.gallery.current_path() is not None)
        self.skip_button.setEnabled(
            self.current_project_id is not None
            and self.actor.role in (ROLE_AI, ROLE_ADMIN)
        )
        for button in (
            self.add_source_files_button,
            self.add_source_folder_button,
            self.add_source_library_button,
            self.crop_source_button,
            self.restore_source_button,
            self.remove_source_button,
            self.clear_sources_button,
        ):
            button.setEnabled(can_edit_sources)
        self.queue_hint.setText(
            f"正在执行 {len(self.active_workers)} 个，等待 {len(self.waiting_jobs)} 个"
        )

    def _enqueue_generation(self) -> None:
        task = self.current_task
        if task is None:
            return
        if (
            self.local_state is None
            or not self.local_state.garments
            or self.local_state.model is None
        ):
            QMessageBox.warning(
                self,
                "本机素材不完整",
                "请至少添加一张衣服图和一张模特图。",
            )
            return
        try:
            if not self.key_store.load():
                raise ValueError("当前账号尚未配置 API Key，请联系管理员")
        except Exception as exc:
            QMessageBox.warning(self, "API Key 不可用", str(exc))
            return
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "提示词为空", "请先填写提示词。")
            return
        model_id = self.model_combo.currentData()
        if model_id == "auto":
            model_id = choose_model(
                prompt=prompt,
                mode="dress_up",
                reference_count=len(self.local_state.references()),
                strategy=AUTO_STRATEGY_QUALITY,
            ).model
        resolution = self.resolution_combo.currentData()
        try:
            attempt = self.repository.claim_and_queue_generation(
                task.id,
                self.actor,
                prompt=prompt,
                model=model_id,
                aspect_ratio=self.aspect_combo.currentData(),
                resolution=None if resolution == "auto" else resolution,
                n=1,
                unit_price=self.repository.current_model_price(model_id),
            )
        except Exception as exc:
            QMessageBox.warning(self, "加入队列失败", str(exc))
            return
        local_references = self.local_state.references()
        if self.local_store is not None:
            try:
                self.local_store.save_attempt_snapshot(attempt.id)
            except Exception as exc:
                try:
                    self.repository.fail_generation_attempt(
                        attempt.id,
                        self.actor,
                        error_code="local_source_snapshot",
                        error_message=str(exc),
                    )
                except Exception:
                    pass
                QMessageBox.warning(
                    self,
                    "本机素材保存失败",
                    str(exc),
                )
                return
        self.waiting_jobs.append(
            {
                "task": task,
                "attempt": attempt,
                "local_references": local_references,
            }
        )
        self.refresh()

    def _resume_queued_jobs(self) -> None:
        try:
            attempts = self.repository.list_queued_attempts(self.actor.id)
        except Exception:
            return
        known = {job["attempt"].id for job in self.waiting_jobs}
        known.update(
            worker.reserved_attempt.id
            for worker in self.active_workers
            if worker.reserved_attempt is not None
        )
        for attempt in attempts:
            if attempt.id in known:
                continue
            task = self.repository.get_task(attempt.task_id)
            if task is None:
                continue
            self.waiting_jobs.append(
                {
                    "task": task,
                    "attempt": attempt,
                    "local_references": None,
                }
            )

    def _pump_queue(self) -> None:
        try:
            limit = self.repository.get_account_policy(
                self.actor.id
            )["max_generation_concurrency"]
        except Exception:
            limit = 2
        while self.waiting_jobs and len(self.active_workers) < limit:
            job = self.waiting_jobs.popleft()
            attempt = job["attempt"]
            local_references = job.get("local_references")
            if local_references is None:
                source_store = LocalTaskSourceStore(
                    self.file_store.paths.root / "local_sources",
                    self.actor.id,
                    job["task"],
                    self.resolver,
                )
                local_references = source_store.load_attempt_snapshot(
                    attempt.id
                )
            worker = ProjectGenerationWorker(
                repository=self.repository,
                client=self.client,
                file_store=self.file_store,
                resolver=self.resolver,
                task=job["task"],
                actor=self.actor,
                prompt=attempt.prompt_text,
                model=attempt.model,
                aspect_ratio=attempt.aspect_ratio,
                resolution=attempt.resolution,
                n=attempt.n,
                local_references=local_references,
                reserved_attempt=attempt,
            )
            worker.signals.finished.connect(
                lambda _task_id, _attempt_id, item=worker: self._job_finished(item)
            )
            worker.signals.failed.connect(
                lambda _task_id, message, _attempt_id, item=worker: (
                    self._job_failed(item, message)
                )
            )
            self.active_workers.add(worker)
            worker.start()
        self._refresh_queue()
        self._update_controls()

    def _job_finished(self, worker: ProjectGenerationWorker) -> None:
        self.active_workers.discard(worker)
        if self.current_task and self.current_task.id == worker.task.id:
            self.refresh()
        self._pump_queue()

    def _job_failed(
        self,
        worker: ProjectGenerationWorker,
        message: str,
    ) -> None:
        self.active_workers.discard(worker)
        self._refresh_queue()
        self._update_controls()
        QMessageBox.warning(self, "生成失败", message)
        self._pump_queue()

    def _refresh_queue(self) -> None:
        if not hasattr(self, "queue_table") or not self.isVisible():
            return
        task = self.current_task
        self.queue_table.setRowCount(0)
        if task is None:
            self.queue_hint.setText(
                f"正在执行 {len(self.active_workers)} 个，等待 {len(self.waiting_jobs)} 个"
            )
            return
        try:
            attempts = self.repository.list_attempts(task.id)
        except Exception:
            attempts = []
        for attempt in attempts:
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            label = "PS成品" if attempt.model == "manual_ps" else ATTEMPT_LABELS.get(
                attempt.status,
                attempt.status,
            )
            values = (
                f"第 {attempt.attempt_no} 轮",
                attempt.operator_name or str(attempt.operator_id),
                label,
                utc_naive_to_local(
                    attempt.created_at
                ).strftime("%Y-%m-%d %H:%M")
                if attempt.created_at
                else "",
                str(attempt.generated_image_count),
            )
            for column, value in enumerate(values):
                self.queue_table.setItem(row, column, QTableWidgetItem(value))
        self.queue_hint.setText(
            f"正在执行 {len(self.active_workers)} 个，等待 {len(self.waiting_jobs)} 个"
        )

    def _load_results(self) -> None:
        task = self.current_task
        if task is None:
            return
        try:
            results = self.repository.list_results(task.id)
        except Exception as exc:
            QMessageBox.warning(self, "读取候选图失败", str(exc))
            return
        self.gallery.set_task(task, results)
        self._selection_changed()

    def _selection_changed(self, *_args) -> None:
        count = len(self.gallery.selected_result_ids())
        self.selection_label.setText(f"已选择 {count} 张最终图")
        self._update_controls()

    def _upload_manual(self) -> None:
        task = self.current_task
        if task is None or not self._can_upload_manual(task):
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择 PS 成品",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        if not paths:
            return
        try:
            count = upload_manual_candidates(
                self.repository,
                self.resolver,
                self.file_store,
                task,
                self.actor,
                [Path(path) for path in paths],
            )
        except Exception as exc:
            QMessageBox.warning(self, "上传失败", str(exc))
            return
        QMessageBox.information(self, "上传完成", f"已添加 {count} 张 PS 成品。")
        self.refresh()

    def _can_upload_manual(self, task: ProjectTask) -> bool:
        if self.actor.role not in (ROLE_AI, ROLE_ADMIN):
            return False
        if task.status in (
            TASK_AWAITING_ACCEPTANCE,
            TASK_APPROVED,
            TASK_CANCELLED,
        ):
            return False
        if task.status == TASK_AVAILABLE:
            return True
        return (
            self._is_assignee(task)
            or (
                task.status == TASK_RETURNED
                and self.actor.role == ROLE_ADMIN
            )
        )

    def _retry_task(self) -> None:
        task = self.current_task
        if task is None:
            return
        self.prompt_edit.setPlainText(task.prompt_text)
        model_index = self.model_combo.findData(task.model)
        if model_index >= 0:
            self.model_combo.setCurrentIndex(model_index)
        if task.aspect_ratio:
            self.aspect_combo.setCurrentText(task.aspect_ratio)
        resolution_index = self.resolution_combo.findData(task.resolution)
        if resolution_index >= 0:
            self.resolution_combo.setCurrentIndex(resolution_index)
        self.count_spin.setValue(max(1, task.n))
        self.prompt_edit.setFocus()

    def _save_result(self) -> None:
        task = self.current_task
        result = self.gallery.current_result()
        source = self.gallery.current_path()
        if task is None or result is None or source is None:
            QMessageBox.information(self, "尚未选择图片", "请先选择一张成品图。")
            return
        directory = self._save_directory(task)
        if directory is None:
            return
        try:
            destination = export_original_image(
                source,
                directory,
                task_code=task.task_code,
                result_index=result.result_index,
            )
            self.app_settings.set_last_folder(directory)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        show_saved_toast(self, destination)

    def _save_directory(self, task: ProjectTask) -> Path | None:
        mode = self.app_settings.local_save_mode()
        directory: Path | None = None
        if mode == LOCAL_SAVE_FIXED:
            candidate = self.app_settings.fixed_folder()
            if candidate and candidate.is_dir():
                directory = candidate
            else:
                QMessageBox.information(
                    self,
                    "固定文件夹不可用",
                    "设置的固定文件夹不存在或不可访问，请重新选择。",
                )
        elif mode == LOCAL_SAVE_REFERENCE:
            product_root = self.resolver.project_root(
                task.project_relative_root
            )
            candidate = product_root.parent / "AI图"
            try:
                candidate.mkdir(parents=True, exist_ok=True)
            except OSError:
                candidate = None
            if candidate and candidate.is_dir():
                directory = candidate
            else:
                QMessageBox.information(
                    self,
                    "需要选择保存位置",
                    "无法在当前任务目录旁创建“AI图”文件夹，请选择本次保存文件夹。",
                )
        if directory is not None:
            return directory
        initial = self.app_settings.last_folder()
        initial_text = str(initial) if initial and initial.is_dir() else ""
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择本地保存文件夹",
            initial_text,
        )
        if not selected:
            return None
        directory = Path(selected).resolve()
        if mode == LOCAL_SAVE_FIXED:
            self.app_settings.set_fixed_folder(directory)
        return directory

    def _compare_result(self) -> None:
        task = self.current_task
        result_path = self.gallery.current_path()
        if task is None or result_path is None:
            QMessageBox.information(self, "尚未选择图片", "请先选择一张成品图。")
            return
        model_path = self.resolver.resolve_project_path(
            task.project_relative_root,
            task.model_path,
        )
        CompareDialog(
            model_path,
            result_path,
            left_label="原模特图",
            right_label="生成成品",
            parent=self,
        ).exec()

    def _skip_remaining(self) -> None:
        if self.current_project_id is None:
            return
        group = self._groups_by_id.get(self.current_project_id)
        if group is None:
            return
        remaining = [
            task
            for task in group.tasks
            if task.status
            in (
                TASK_AVAILABLE,
                TASK_CLAIMED,
                TASK_WORKING,
                TASK_RETURNED,
            )
        ]
        if not remaining:
            QMessageBox.information(
                self,
                "没有剩余子任务",
                "当前主任务没有可标记为无需制作的子任务。",
            )
            return
        reason, ok = QInputDialog.getMultiLineText(
            self,
            "剩余不制作",
            "请填写剩余子任务不制作的原因：",
        )
        if not ok:
            return
        if not reason.strip():
            QMessageBox.warning(self, "需要原因", "请填写不制作原因。")
            return
        answer = QMessageBox.question(
            self,
            "确认剩余不制作",
            f"将把当前主任务中 {len(remaining)} 个未完成子任务"
            "标记为“无需制作”，是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self.repository.skip_remaining_subtasks(
                self.current_project_id,
                self.actor,
                reason=reason,
            )
        except Exception as exc:
            QMessageBox.warning(self, "操作失败", str(exc))
            return
        QMessageBox.information(
            self,
            "操作完成",
            f"已将 {count} 个子任务标记为无需制作。",
        )
        self.refresh()

    def _submit(self) -> None:
        task = self.current_task
        if task is None:
            return
        result_ids = self.gallery.selected_result_ids()
        if not result_ids:
            QMessageBox.warning(self, "未选择成品", "请至少选择一张最终图。")
            return
        answer = QMessageBox.question(
            self,
            "提交运营验收",
            f"确认提交选中的 {len(result_ids)} 张图片吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.submit_for_acceptance(
                task.id,
                self.actor,
                final_result_ids=result_ids,
                final_quantity=len(result_ids),
            )
        except Exception as exc:
            QMessageBox.warning(self, "提交失败", str(exc))
            return
        self.refresh()

    def _refresh_templates(self) -> None:
        if not hasattr(self, "template_combo"):
            return
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("选择提示词模板", None)
        try:
            templates = self.repository.list_templates(self.actor)
        except Exception:
            templates = []
        for template in templates:
            scope = "共享" if template["scope"] == "shared" else "个人"
            self.template_combo.addItem(
                f"[{scope}] {template['name']}",
                template,
            )
        self.template_combo.blockSignals(False)

    def _apply_template(self) -> None:
        template = self.template_combo.currentData()
        if template:
            self.prompt_edit.setPlainText(template["prompt"])

    def _save_template(self) -> None:
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "提示词为空", "请先填写提示词。")
            return
        name, ok = QInputDialog.getText(self, "保存个人模板", "模板名称")
        if not ok or not name.strip():
            return
        try:
            self.repository.save_template(
                self.actor,
                scope="personal",
                name=name,
                prompt=prompt,
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._refresh_templates()

    def is_generating(self) -> bool:
        return bool(self.active_workers or self.waiting_jobs)
