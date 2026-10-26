from __future__ import annotations

from collections import deque
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
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
    TASK_CLAIMED,
    TASK_RETURNED,
    TASK_STATUS_LABELS,
    TASK_WORKING,
    AuthUser,
    ProjectTask,
)
from ..collaboration.worker import ProjectGenerationWorker
from ..constants import (
    ASPECT_RATIOS,
    MODEL_LABELS,
    MODEL_SELECTION_OPTIONS,
    RESOLUTIONS,
)
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
from .dialogs import CompareDialog
from .result_gallery import ResultGallery
from .save_helpers import show_saved_toast
from .widgets import load_scaled_pixmap


ATTEMPT_LABELS = {
    ATTEMPT_QUEUED: "排队中",
    ATTEMPT_RUNNING: "生成中",
    ATTEMPT_SUCCEEDED: "成功",
    ATTEMPT_FAILED: "失败",
    "manual_ps": "PS成品",
}


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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.client = client
        self.key_store = key_store
        self.file_store = file_store
        self.app_settings = app_settings
        self.tasks: list[ProjectTask] = []
        self.current_task: ProjectTask | None = None
        self.active_workers: set[ProjectGenerationWorker] = set()
        self.waiting_jobs: deque[dict] = deque()
        self._build()
        self.queue_timer = QTimer(self)
        self.queue_timer.setInterval(1200)
        self.queue_timer.timeout.connect(self._refresh_queue)
        self.queue_timer.start()
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
        self.include_finished.toggled.connect(self.refresh)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        header.addWidget(self.include_finished)
        header.addWidget(refresh)
        outer.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_task_panel())
        splitter.addWidget(self._build_generation_panel())
        splitter.addWidget(self._build_candidate_panel())
        splitter.setSizes([330, 520, 500])
        outer.addWidget(splitter, 1)

    def _build_task_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        title = QLabel("任务列表")
        title.setStyleSheet("font-size:16px;font-weight:700;")
        layout.addWidget(title)
        self.task_table = QTableWidget(0, 4)
        self.task_table.setHorizontalHeaderLabels(
            ["子任务", "货号", "状态", "制作人员"]
        )
        self.task_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.task_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.task_table.horizontalHeader().setStretchLastSection(True)
        self.task_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.task_table.itemSelectionChanged.connect(self._task_selected)
        vertical = QSplitter(Qt.Orientation.Vertical)
        task_host = QWidget()
        task_layout = QVBoxLayout(task_host)
        task_layout.setContentsMargins(0, 0, 0, 0)
        task_layout.addWidget(self.task_table, 1)
        vertical.addWidget(task_host)
        queue_card = QGroupBox("生成任务队列")
        queue_layout = QVBoxLayout(queue_card)
        self.queue_table = QTableWidget(0, 4)
        self.queue_table.setHorizontalHeaderLabels(
            ["轮次", "操作人员", "状态", "时间"]
        )
        self.queue_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.queue_table.horizontalHeader().setStretchLastSection(True)
        queue_layout.addWidget(self.queue_table)
        self.queue_hint = QLabel()
        self.queue_hint.setStyleSheet("color:#60708a;")
        queue_layout.addWidget(self.queue_hint)
        vertical.addWidget(queue_card)
        vertical.setSizes([360, 260])
        layout.addWidget(vertical, 1)
        return panel

    def _build_generation_panel(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(8, 0, 8, 0)
        info_title = QLabel("子任务信息与参考图")
        info_title.setStyleSheet("font-size:16px;font-weight:700;")
        top_layout.addWidget(info_title)
        self.task_detail = QLabel("选择一个子任务")
        self.task_detail.setWordWrap(True)
        self.task_detail.setStyleSheet("color:#60708a;")
        top_layout.addWidget(self.task_detail)
        images_row = QHBoxLayout()
        garment_box = QGroupBox("衣服图")
        garment_layout = QVBoxLayout(garment_box)
        self.garment_list = QListWidget()
        self.garment_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.garment_list.setIconSize(QSize(104, 104))
        self.garment_list.setGridSize(QSize(130, 136))
        self.garment_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        garment_layout.addWidget(self.garment_list)
        model_box = QGroupBox("模特图")
        model_layout = QVBoxLayout(model_box)
        self.model_preview = QLabel("尚未选择任务")
        self.model_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.model_preview.setMinimumSize(220, 240)
        self.model_preview.setStyleSheet(
            "background:#f3f6fb;border:1px solid #dbe3ef;border-radius:6px;"
        )
        self.model_name = QLabel()
        self.model_name.setWordWrap(True)
        model_layout.addWidget(self.model_preview, 1)
        model_layout.addWidget(self.model_name)
        images_row.addWidget(garment_box, 3)
        images_row.addWidget(model_box, 2)
        top_layout.addLayout(images_row, 1)
        splitter.addWidget(top)

        bottom = QWidget()
        layout = QVBoxLayout(bottom)
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
        form = QFormLayout()
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
        form.addRow("模型", self.model_combo)
        form.addRow("宽高比", self.aspect_combo)
        form.addRow("分辨率", self.resolution_combo)
        form.addRow("每次数量", self.count_spin)
        prompt_layout.addLayout(form)
        action_row = QHBoxLayout()
        self.generate_button = QPushButton("加入生成队列")
        self.generate_button.setObjectName("PrimaryButton")
        self.generate_button.clicked.connect(self._enqueue_generation)
        action_row.addStretch(1)
        action_row.addWidget(self.generate_button)
        prompt_layout.addLayout(action_row)
        layout.addWidget(prompt_card)
        splitter.addWidget(bottom)
        splitter.setSizes([520, 300])
        return splitter

    def _build_candidate_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        title = QLabel("候选图与 PS 成品")
        title.setStyleSheet("font-size:16px;font-weight:700;")
        layout.addWidget(title)
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
        self.submit_button.setObjectName("PrimaryButton")
        self.retry_button.clicked.connect(self._retry_task)
        self.save_button.clicked.connect(self._save_result)
        self.upload_button.clicked.connect(self._upload_manual)
        self.compare_button.clicked.connect(self._compare_result)
        self.submit_button.clicked.connect(self._submit)
        for button in (
            self.retry_button,
            self.save_button,
            self.upload_button,
            self.compare_button,
            self.submit_button,
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
                include_finished=self.include_finished.isChecked(),
                limit=10000,
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            return
        self._refresh_templates()
        selected_id = self.current_task.id if self.current_task else None
        self.task_table.setRowCount(0)
        selected_row = -1
        for row, task in enumerate(self.tasks):
            self.task_table.insertRow(row)
            values = (
                task.task_code,
                task.product_code or task.project_name,
                TASK_STATUS_LABELS.get(task.status, task.status),
                task.assignee_name or "未分配",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, task.id)
                if task.is_urgent:
                    item.setBackground(Qt.GlobalColor.yellow)
                self.task_table.setItem(row, column, item)
            if task.id == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.task_table.selectRow(selected_row)
        elif self.tasks:
            self.task_table.selectRow(0)
        else:
            self.current_task = None
            self.task_detail.setText("当前没有可查看的任务")
            self.garment_list.clear()
            self.model_preview.clear()
            self.model_name.clear()
            self.gallery.clear()
        self._resume_queued_jobs()
        self._pump_queue()
        self._update_controls()

    def _task_selected(self) -> None:
        row = self.task_table.currentRow()
        self.current_task = (
            self.tasks[row] if 0 <= row < len(self.tasks) else None
        )
        task = self.current_task
        self.garment_list.clear()
        self.model_preview.clear()
        self.model_name.clear()
        if task is None:
            self.gallery.clear()
            self._update_controls()
            return
        self.task_detail.setText(
            f"{task.task_code}\n货号：{task.product_code or task.project_name}　"
            f"制作：{task.assignee_name or '未分配'}\n"
            f"参考顺序：衣服图 {len(task.garment_assets)} 张，最后一张模特图。"
        )
        for asset in task.garment_assets:
            self._add_garment_reference(
                task,
                asset.relative_path,
                asset.original_name,
            )
        self._show_model_reference(task)
        self.prompt_edit.setPlainText(task.prompt_text)
        if task.model in MODEL_LABELS:
            self.model_combo.setCurrentIndex(self.model_combo.findData(task.model))
        if task.aspect_ratio:
            self.aspect_combo.setCurrentText(task.aspect_ratio)
        if task.resolution:
            self.resolution_combo.setCurrentIndex(
                self.resolution_combo.findData(task.resolution)
            )
        self.count_spin.setValue(max(1, task.n))
        self._load_results()
        self._refresh_queue()
        self._update_controls()

    def _add_garment_reference(
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
        pixmap = load_scaled_pixmap(path, QSize(92, 92))
        if not pixmap.isNull():
            item.setIcon(QIcon(pixmap))
        self.garment_list.addItem(item)

    def _show_model_reference(self, task: ProjectTask) -> None:
        path = self.resolver.resolve_project_path(
            task.project_relative_root,
            task.model_path,
        )
        pixmap = load_scaled_pixmap(path, QSize(320, 360))
        if pixmap.isNull():
            self.model_preview.setText("模特图不存在")
        else:
            self.model_preview.setPixmap(pixmap)
        self.model_name.setText(task.model_name)

    def _is_assignee(self, task: ProjectTask | None) -> bool:
        return bool(task and task.assignee_id == self.actor.id)

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
        self.generate_button.setEnabled(can_claim or can_work)
        self.upload_button.setEnabled(can_work and not task_busy)
        self.submit_button.setEnabled(
            can_work and not task_busy and bool(self.gallery.selected_result_ids())
        )
        self.retry_button.setEnabled(can_work)
        self.save_button.setEnabled(self.gallery.current_path() is not None)
        self.compare_button.setEnabled(self.gallery.current_path() is not None)
        self.queue_hint.setText(
            f"正在执行 {len(self.active_workers)} 个，等待 {len(self.waiting_jobs)} 个"
        )

    def _enqueue_generation(self) -> None:
        task = self.current_task
        if task is None:
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
                reference_count=len(task.garment_assets) + 1,
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
                n=self.count_spin.value(),
                unit_price=self.repository.current_model_price(model_id),
            )
        except Exception as exc:
            QMessageBox.warning(self, "加入队列失败", str(exc))
            return
        self.waiting_jobs.append({"task": task, "attempt": attempt})
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
            self.waiting_jobs.append({"task": task, "attempt": attempt})

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
                    attempt.finished_at or attempt.created_at
                ).strftime("%H:%M:%S") if attempt.created_at else "",
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
        if task is None or not self._is_assignee(task):
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
