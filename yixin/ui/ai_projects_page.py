from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..api import NanoBananaClient
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    PRIORITY_LABELS,
    ROLE_ADMIN,
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
from ..security import SecretStore
from ..settings import AUTO_STRATEGY_QUALITY
from ..storage import FileStore
from ..time_utils import utc_naive_to_local
from .result_gallery import ResultGallery
from .widgets import load_scaled_pixmap


class AiProjectsPage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        client: NanoBananaClient,
        secret_store: SecretStore,
        file_store: FileStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.client = client
        self.secret_store = secret_store
        self.file_store = file_store
        self.tasks: list[ProjectTask] = []
        self.current_task: ProjectTask | None = None
        self.worker: ProjectGenerationWorker | None = None
        self._build()
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("AI 项目制作")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel("领取全部人员可见的任务，写提示词、生成并完成自审。")
        subtitle.setStyleSheet("color:#6d7a91;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.include_finished = QCheckBox("显示已完成")
        self.include_finished.toggled.connect(self.refresh)
        refresh_button = QPushButton("刷新任务")
        refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.include_finished)
        header.addWidget(refresh_button)
        outer.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.task_table = QTableWidget(0, 8)
        self.task_table.setHorizontalHeaderLabels(
            [
                "任务",
                "项目",
                "服装图",
                "模特图",
                "截止时间",
                "优先级",
                "制作人员",
                "状态",
            ]
        )
        self.task_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.task_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.task_table.horizontalHeader().setStretchLastSection(True)
        self.task_table.itemSelectionChanged.connect(self._task_selected)
        splitter.addWidget(self.task_table)

        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.detail = QWidget()
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(0, 8, 4, 8)
        self.task_title = QLabel("选择一个任务")
        self.task_title.setStyleSheet("font-size:17px;font-weight:700;")
        detail_layout.addWidget(self.task_title)
        self.task_meta = QLabel()
        self.task_meta.setWordWrap(True)
        self.task_meta.setStyleSheet("color:#60708a;")
        detail_layout.addWidget(self.task_meta)

        source_row = QHBoxLayout()
        self.garment_preview = self._image_card("服装图")
        self.model_preview = self._image_card("模特图")
        source_row.addWidget(self.garment_preview[0], 1)
        source_row.addWidget(self.model_preview[0], 1)
        detail_layout.addLayout(source_row)

        prompt_card = QGroupBox("提示词与生成参数")
        prompt_layout = QVBoxLayout(prompt_card)
        template_row = QHBoxLayout()
        self.template_combo = QComboBox()
        self.template_combo.addItem("选择提示词模板", None)
        self.template_combo.currentIndexChanged.connect(self._apply_template)
        save_template = QPushButton("保存个人模板")
        save_template.clicked.connect(self._save_personal_template)
        template_row.addWidget(self.template_combo, 1)
        template_row.addWidget(save_template)
        prompt_layout.addLayout(template_row)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText(
            "描述换装要求，例如：将第 1 张图的服装自然穿到第 2 张图人物身上，"
            "保持身份、姿态和背景不变，准确还原面料、颜色和图案。"
        )
        self.prompt_edit.setMinimumHeight(130)
        prompt_layout.addWidget(self.prompt_edit)
        params = QFormLayout()
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
        params.addRow("模型", self.model_combo)
        params.addRow("宽高比", self.aspect_combo)
        params.addRow("分辨率", self.resolution_combo)
        params.addRow("本次生成数量", self.count_spin)
        prompt_layout.addLayout(params)
        detail_layout.addWidget(prompt_card)

        action_row = QHBoxLayout()
        self.claim_button = QPushButton("领取任务")
        self.claim_button.setObjectName("PrimaryButton")
        self.generate_button = QPushButton("生成图片")
        self.generate_button.setObjectName("PrimaryButton")
        self.finalize_button = QPushButton("提交运营验收")
        self.claim_button.clicked.connect(self._claim_task)
        self.generate_button.clicked.connect(self._generate)
        self.finalize_button.clicked.connect(self._submit_final)
        action_row.addWidget(self.claim_button)
        action_row.addWidget(self.generate_button)
        action_row.addStretch(1)
        action_row.addWidget(self.finalize_button)
        detail_layout.addLayout(action_row)

        gallery_card = QGroupBox("生成候选图（勾选最终交付图）")
        gallery_layout = QVBoxLayout(gallery_card)
        gallery_toolbar = QHBoxLayout()
        self.final_count_label = QLabel("已选择 0 张最终图")
        refresh_results = QPushButton("刷新结果")
        refresh_results.clicked.connect(self._load_results)
        gallery_toolbar.addWidget(self.final_count_label, 1)
        gallery_toolbar.addWidget(refresh_results)
        gallery_layout.addLayout(gallery_toolbar)
        self.gallery = ResultGallery(self.resolver, checkable=True)
        self.gallery.list_widget.itemChanged.connect(
            self._final_selection_changed
        )
        gallery_layout.addWidget(self.gallery, 1)
        detail_layout.addWidget(gallery_card, 1)
        detail_scroll.setWidget(self.detail)
        splitter.addWidget(detail_scroll)
        splitter.setSizes([270, 650])
        outer.addWidget(splitter, 1)

    @staticmethod
    def _image_card(title: str) -> tuple[QWidget, QLabel]:
        widget = QGroupBox(title)
        layout = QVBoxLayout(widget)
        image = QLabel("尚未选择任务")
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setMinimumHeight(170)
        image.setStyleSheet(
            "background:#f3f6fb;border:1px solid #dbe3ef;border-radius:6px;"
        )
        name = QLabel()
        name.setWordWrap(True)
        layout.addWidget(image, 1)
        layout.addWidget(name)
        return widget, image

    def refresh(self) -> None:
        try:
            shared_root = self.repository.get_shared_setting("share_root")
            if shared_root:
                self.resolver = SharedPathResolver(shared_root)
            self.tasks = self.repository.list_board_tasks(
                self.actor,
                include_finished=self.include_finished.isChecked(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            return
        self._refresh_template_combo()
        selected_id = self.current_task.id if self.current_task else None
        self.task_table.setRowCount(0)
        selected_row = -1
        for index, task in enumerate(self.tasks):
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            values = (
                task.task_code,
                f"{task.project_code} · {task.project_name}",
                task.garment_name,
                task.model_name,
                utc_naive_to_local(task.project_deadline).strftime(
                    "%Y-%m-%d %H:%M"
                )
                if task.project_deadline
                else "",
                PRIORITY_LABELS.get(task.project_priority, task.project_priority),
                task.assignee_name or "未分配",
                TASK_STATUS_LABELS.get(task.status, task.status),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, task.id)
                self.task_table.setItem(row, column, item)
            if task.id == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.task_table.selectRow(selected_row)
        elif self.tasks:
            self.task_table.selectRow(0)
        else:
            self.current_task = None
            self.task_title.setText("当前没有可处理任务")
            self._update_controls()

    def is_generating(self) -> bool:
        return bool(self.worker and self.worker.is_running())

    def _current_task(self) -> ProjectTask | None:
        row = self.task_table.currentRow()
        if not 0 <= row < len(self.tasks):
            return None
        return self.tasks[row]

    def _task_selected(self) -> None:
        task = self._current_task()
        self.current_task = task
        if task is None:
            self._update_controls()
            return
        self.task_title.setText(f"{task.task_code} · {task.project_name}")
        deadline_text = (
            utc_naive_to_local(task.project_deadline).strftime(
                "%Y-%m-%d %H:%M"
            )
            if task.project_deadline
            else "未设置"
        )
        self.task_meta.setText(
            f"状态：{TASK_STATUS_LABELS.get(task.status, task.status)}　"
            f"制作人员：{task.assignee_name or '未分配'}\n"
            f"截止：{deadline_text}　"
            f"项目：{task.project_code}"
        )
        self.prompt_edit.setPlainText(task.prompt_text)
        if task.model in MODEL_LABELS:
            self.model_combo.setCurrentIndex(
                self.model_combo.findData(task.model)
            )
        if task.aspect_ratio:
            self.aspect_combo.setCurrentText(task.aspect_ratio)
        if task.resolution:
            self.resolution_combo.setCurrentIndex(
                self.resolution_combo.findData(task.resolution)
            )
        self.count_spin.setValue(task.n)
        self._show_source_image(
            self.garment_preview[1],
            task.project_relative_root,
            task.garment_path,
            task.garment_name,
        )
        self._show_source_image(
            self.model_preview[1],
            task.project_relative_root,
            task.model_path,
            task.model_name,
        )
        self._load_results()
        self._update_controls()

    def _show_source_image(
        self,
        label: QLabel,
        project_root: str,
        relative_path: str,
        name: str,
    ) -> None:
        path = self.resolver.resolve_project_path(
            project_root,
            relative_path,
        )
        pixmap = load_scaled_pixmap(path, QSize(420, 260))
        if pixmap.isNull():
            label.setText(f"图片不可用\n{path}")
        else:
            label.setPixmap(pixmap)
        label.setToolTip(f"{name}\n{path}")

    def _update_controls(self) -> None:
        task = self.current_task
        running = bool(self.worker and self.worker.is_running())
        is_mine = bool(task and task.assignee_id == self.actor.id)
        can_claim = bool(
            task
            and task.status in (TASK_AVAILABLE, TASK_RETURNED)
            and (
                task.status == TASK_AVAILABLE
                or is_mine
                or self.actor.role == ROLE_ADMIN
            )
        )
        can_work = bool(
            is_mine
            and task
            and task.status in (TASK_CLAIMED, TASK_WORKING, TASK_RETURNED)
            and not running
        )
        self.claim_button.setEnabled(can_claim and not running)
        self.generate_button.setEnabled(can_work)
        self.finalize_button.setEnabled(
            can_work and bool(self.gallery.selected_result_ids())
        )
        if running:
            self.generate_button.setText("生成中…")
        else:
            self.generate_button.setText("生成图片")

    def _claim_task(self) -> None:
        task = self.current_task
        if task is None:
            return
        try:
            self.repository.claim_task(task.id, self.actor)
        except Exception as exc:
            QMessageBox.warning(self, "领取失败", str(exc))
            self.refresh()
            return
        self.refresh()

    def _generate(self) -> None:
        task = self.current_task
        if task is None or (self.worker and self.worker.is_running()):
            return
        if not self.secret_store.load():
            QMessageBox.warning(
                self,
                "未配置 API Key",
                "请先在设置中配置生图 API Key。",
            )
            return
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "提示词为空", "请先填写换装提示词。")
            return
        model_id = self.model_combo.currentData()
        if model_id == "auto":
            decision = choose_model(
                prompt=prompt,
                mode="dress_up",
                reference_count=2,
                strategy=AUTO_STRATEGY_QUALITY,
            )
            model_id = decision.model
        resolution = self.resolution_combo.currentData()
        self.worker = ProjectGenerationWorker(
            repository=self.repository,
            client=self.client,
            file_store=self.file_store,
            resolver=self.resolver,
            task=task,
            actor=self.actor,
            prompt=prompt,
            model=model_id,
            aspect_ratio=self.aspect_combo.currentData(),
            resolution=None if resolution == "auto" else resolution,
            n=self.count_spin.value(),
        )
        self.worker.signals.finished.connect(self._generation_finished)
        self.worker.signals.failed.connect(self._generation_failed)
        self.worker.start()
        self._update_controls()

    def _generation_finished(self, task_id: int, _attempt_id: int) -> None:
        if self.current_task and self.current_task.id == task_id:
            self.refresh()
        self._update_controls()
        QMessageBox.information(self, "生成完成", "候选图片已写入共享盘。")

    def _generation_failed(
        self,
        _task_id: int,
        message: str,
        _attempt_id: str,
    ) -> None:
        self._update_controls()
        QMessageBox.warning(self, "生成失败", message)

    def _load_results(self) -> None:
        task = self.current_task
        if task is None:
            return
        try:
            results = self.repository.list_results(task.id)
        except Exception as exc:
            QMessageBox.warning(self, "读取结果失败", str(exc))
            return
        self.gallery.set_task(task, results)
        self._final_selection_changed()

    def _final_selection_changed(self, *_args) -> None:
        count = len(self.gallery.selected_result_ids())
        self.final_count_label.setText(f"已选择 {count} 张最终图")
        self._update_controls()

    def _submit_final(self) -> None:
        task = self.current_task
        if task is None:
            return
        result_ids = self.gallery.selected_result_ids()
        if not result_ids:
            QMessageBox.warning(self, "未选择成品", "请至少勾选一张最终图片。")
            return
        answer = QMessageBox.question(
            self,
            "提交运营验收",
            f"确认将选中的 {len(result_ids)} 张图片作为最终成品提交吗？",
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

    def _refresh_template_combo(self) -> None:
        if not hasattr(self, "template_combo"):
            return
        current_id = self.template_combo.currentData()
        current_id = current_id["id"] if current_id else None
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
            if current_id and template["id"] == current_id:
                self.template_combo.setCurrentIndex(
                    self.template_combo.count() - 1
                )
        self.template_combo.blockSignals(False)

    def _apply_template(self) -> None:
        template = self.template_combo.currentData()
        if template:
            self.prompt_edit.setPlainText(template["prompt"])

    def _save_personal_template(self) -> None:
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
        self._refresh_template_combo()
