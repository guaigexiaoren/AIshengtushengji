from __future__ import annotations

from collections import deque

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..api import NanoBananaClient
from ..collaboration.local_sources import LocalTaskSourceStore
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    TASK_STATUS_LABELS,
    AuthUser,
    GenerationAttempt,
    GenerationPairSpec,
    ProjectTask,
)
from ..collaboration.worker import ProjectGenerationWorker
from ..constants import (
    AUTO_MODEL_ID,
    BUILTIN_PROMPT_TEMPLATES,
    MODEL_LABELS,
)
from ..database import Database
from ..library import LibraryService
from ..model_router import choose_model
from ..security import SecretStore
from ..settings import AUTO_STRATEGY_QUALITY
from ..storage import FileStore
from ..worker import TaskWorker
from .batch_dress_page import BatchDressPage, DressTaskGroup


class AIGenerationPage(BatchDressPage):
    project_attempt_cancelled = Signal(int)

    def __init__(
        self,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        client: NanoBananaClient,
        key_store,
        database: Database,
        file_store: FileStore,
        library_service: LibraryService,
        worker: TaskWorker,
        secret_store: SecretStore,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
    ) -> None:
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.client = client
        self.key_store = key_store
        self.generation_tasks: list[ProjectTask] = []
        self._generation_tasks_by_id: dict[int, ProjectTask] = {}
        self.current_task: ProjectTask | None = None
        self.local_store: LocalTaskSourceStore | None = None
        self.active_workers: set[ProjectGenerationWorker] = set()
        self.waiting_jobs: deque[dict] = deque()
        self.api_providers = []
        self.project_progress: dict[int, tuple[int, int]] = {}
        self.force_direct_retry_task_id: int | None = None
        super().__init__(
            database,
            file_store,
            library_service,
            worker,
            secret_store,
            parent,
            allow_pair_skip=True,
            allow_bottom_skip=True,
            generate_button_text="加入队列",
        )
        self.task_selector.currentIndexChanged.connect(
            self._task_changed
        )
        self.task_refresh_button.clicked.connect(self.refresh)
        self._refresh_providers()
        self.queue_timer = QTimer(self)
        self.queue_timer.setInterval(1200)
        self.queue_timer.timeout.connect(self._refresh_queue_state)
        self.queue_timer.start()
        if auto_load:
            self.refresh()

    def _build_header(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        title_row = QHBoxLayout()
        title = QLabel("AI生图")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(QLabel("子任务"))
        self.task_selector = QComboBox()
        self.task_selector.setMinimumWidth(420)
        title_row.addWidget(self.task_selector)
        self.task_refresh_button = QPushButton("刷新子任务")
        title_row.addWidget(self.task_refresh_button)
        self.task_status_label = QLabel("尚未选择")
        self.task_status_label.setStyleSheet("color:#60708a;")
        title_row.addWidget(self.task_status_label)
        subtitle = QLabel(
            "选择子任务后自动载入共享盘素材，编辑配对并批量加入生成队列。"
        )
        subtitle.setStyleSheet("color:#6d7a91;")
        layout.addLayout(title_row)
        layout.addWidget(subtitle)
        return card

    def _additional_settings_rows(self, form: QFormLayout) -> None:
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("自动选择", "")
        form.addRow("供应商", self.provider_combo)
        template_host = QWidget()
        template_layout = QHBoxLayout(template_host)
        template_layout.setContentsMargins(0, 0, 0, 0)
        self._build_template_controls(template_layout)
        form.addRow("提示词模板", template_host)

    def _build_prompt(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        title = QLabel("提示词")
        title.setStyleSheet("font-weight:700;")
        layout.addWidget(title)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setMinimumHeight(130)
        self.prompt_edit.setPlaceholderText(
            "请描述换装要求。参考图顺序固定为：衣服图在前，模特图最后。"
        )
        layout.addWidget(self.prompt_edit)
        return card

    def _refresh_providers(self) -> None:
        selected = self.provider_combo.currentData()
        try:
            self.api_providers = self.repository.list_api_providers(
                include_disabled=False
            )
        except Exception:
            self.api_providers = []
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        self.provider_combo.addItem("自动选择", "")
        for provider in self.api_providers:
            self.provider_combo.addItem(
                provider.display_name,
                provider.provider_code,
            )
        index = self.provider_combo.findData(selected or "")
        self.provider_combo.setCurrentIndex(max(0, index))
        self.provider_combo.blockSignals(False)

    def refresh(self) -> None:
        self._refresh_providers()
        self.refresh_templates()
        self._refresh_generation_tasks()
        self._resume_queued_jobs()
        self._pump_queue()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._resume_queued_jobs()
        self._pump_queue()

    def _refresh_generation_tasks(self) -> None:
        try:
            tasks = self.repository.list_ai_generation_tasks(
                self.actor
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取AI生图任务失败", str(exc))
            return
        current_id = self.task_selector.currentData()
        self.generation_tasks = tasks
        try:
            self.project_progress = (
                self.repository.project_progress_counts(
                    tuple(task.project_id for task in tasks)
                )
            )
        except Exception:
            self.project_progress = {}
        self._generation_tasks_by_id = {
            task.id: task for task in tasks
        }
        self.task_selector.blockSignals(True)
        self.task_selector.clear()
        self.task_selector.addItem("请选择子任务", None)
        for task in tasks:
            completed, total = self.project_progress.get(
                task.project_id,
                (0, 0),
            )
            label = (
                f"{task.task_code} · {task.product_code or task.project_name}"
                f" · 主任务 {completed}/{total}"
                f" · {TASK_STATUS_LABELS.get(task.status, task.status)}"
            )
            if task.parent_task_id is not None:
                label += " · 失败重试"
            self.task_selector.addItem(label, task.id)
        target = (
            self.task_selector.findData(current_id)
            if current_id is not None
            else (1 if tasks else 0)
        )
        self.task_selector.setCurrentIndex(max(0, target))
        self.task_selector.blockSignals(False)
        if target < 0:
            self._clear_generation_editor()
        elif current_id is None:
            self._task_changed()
        else:
            self.current_task = self._generation_tasks_by_id.get(
                current_id
            )

    def _clear_generation_editor(self) -> None:
        self.current_task = None
        self.local_store = None
        self.garment_assets = []
        self.person_assets = []
        self.pairs = []
        self._refresh_role("garment")
        self._refresh_role("person")
        self._refresh_pairs()
        self.prompt_edit.clear()
        self.task_status_label.setText("没有可制作的子任务")
        self._update_asset_actions()

    def _task_changed(self, _index: int | None = None) -> None:
        self._persist_current_sources()
        task_id = self.task_selector.currentData()
        task = self._generation_tasks_by_id.get(task_id)
        if task is None:
            self._clear_generation_editor()
            return
        self.current_task = task
        try:
            self.local_store = LocalTaskSourceStore(
                self.file_store.paths.root / "local_sources",
                self.actor.id,
                task,
                self.resolver,
            )
            state = self.local_store.state
        except Exception as exc:
            QMessageBox.warning(self, "读取任务素材失败", str(exc))
            self._clear_generation_editor()
            return
        self.garment_assets = list(state.garments)
        self.person_assets = [state.model] if state.model else []
        self.selected_garment = None
        self.pairs = []
        self._refresh_role("garment")
        self._refresh_role("person")
        self._refresh_pairs()
        self._auto_match()
        prompt = task.prompt_text.strip()
        if not prompt:
            prompt = BUILTIN_PROMPT_TEMPLATES[0][1]
        self.prompt_edit.setPlainText(prompt)
        if task.model in MODEL_LABELS:
            index = self.model_combo.findData(task.model)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
        aspect_index = self.aspect_combo.findData(task.aspect_ratio)
        if aspect_index >= 0:
            self.aspect_combo.setCurrentIndex(aspect_index)
        resolution_index = self.resolution_combo.findData(
            task.resolution or "auto"
        )
        if resolution_index >= 0:
            self.resolution_combo.setCurrentIndex(resolution_index)
        self.count_spin.setValue(max(1, int(task.n)))
        self.task_status_label.setText(
            f"主任务 "
            f"{self.project_progress.get(task.project_id, (0, 0))[0]}/"
            f"{self.project_progress.get(task.project_id, (0, 0))[1]}"
            f" · {TASK_STATUS_LABELS.get(task.status, task.status)}"
        )
        self._update_asset_actions()

    def _persist_current_sources(self) -> None:
        if (
            self.current_task is None
            or self.local_store is None
            or not self.garment_assets
            or len(self.person_assets) != 1
        ):
            return
        try:
            self.local_store.replace_all(
                self.garment_assets,
                self.person_assets[0],
            )
        except Exception:
            return

    def _enqueue_all(self) -> None:
        task = self.current_task
        if task is None:
            QMessageBox.information(
                self,
                "未选择子任务",
                "请先在顶部选择需要制作的子任务。",
            )
            return
        if not self.pairs:
            QMessageBox.information(
                self,
                "没有配对",
                "请至少建立一组服装与模特配对。",
            )
            return
        active_pairs = [pair for pair in self.pairs if not pair.skipped]
        prompt = self.prompt_edit.toPlainText().strip()
        if active_pairs and not prompt:
            QMessageBox.information(
                self,
                "缺少提示词",
                "请先填写换装提示词。",
            )
            return
        if active_pairs:
            try:
                if not self.key_store.load():
                    raise ValueError(
                        "当前账号尚未配置 API Key，请联系管理员"
                    )
            except Exception as exc:
                QMessageBox.warning(self, "API Key 不可用", str(exc))
                return
        try:
            if (
                task.parent_task_id is None
                and self.force_direct_retry_task_id != task.id
            ):
                count = self._split_source_task(task, prompt)
            else:
                count = self._retry_child_task(task, prompt)
        except Exception as exc:
            QMessageBox.warning(self, "加入队列失败", str(exc))
            return
        self.pairs.clear()
        self._refresh_pairs()
        self.tasks_enqueued.emit(count)
        self.feedback_label.setText(
            f"已处理 {count} 个配对任务。"
        )
        self._refresh_generation_tasks()
        self._resume_queued_jobs()
        self._pump_queue()
        self.force_direct_retry_task_id = None

    def prepare_history_retry(
        self,
        task_id: int,
        attempt_id: int,
    ) -> None:
        task = self.repository.get_task(task_id)
        if task is None:
            raise ValueError("项目子任务不存在")
        attempts = self.repository.list_attempts(task_id)
        attempt = next(
            (item for item in attempts if item.id == attempt_id),
            None,
        )
        if attempt is None:
            raise ValueError("生成轮次不存在")
        self._generation_tasks_by_id[task.id] = task
        self.generation_tasks = [
            item for item in self.generation_tasks if item.id != task.id
        ] + [task]
        if self.task_selector.findData(task.id) < 0:
            self.task_selector.addItem(
                f"{task.task_code} · 历史重试",
                task.id,
            )
        self.task_selector.setCurrentIndex(
            self.task_selector.findData(task.id)
        )
        self._task_changed()
        self.prompt_edit.setPlainText(attempt.prompt_text)
        model_index = self.model_combo.findData(attempt.model)
        if model_index >= 0:
            self.model_combo.setCurrentIndex(model_index)
        aspect_index = self.aspect_combo.findData(attempt.aspect_ratio)
        if aspect_index >= 0:
            self.aspect_combo.setCurrentIndex(aspect_index)
        resolution_index = self.resolution_combo.findData(
            attempt.resolution or "auto"
        )
        if resolution_index >= 0:
            self.resolution_combo.setCurrentIndex(resolution_index)
        self.count_spin.setValue(max(1, int(attempt.n)))
        provider_index = self.provider_combo.findData(
            attempt.requested_provider_code or ""
        )
        self.provider_combo.setCurrentIndex(max(0, provider_index))
        self.force_direct_retry_task_id = task.id

    def remove_cancelled_attempt(self, attempt_id: int) -> None:
        self.waiting_jobs = deque(
            job
            for job in self.waiting_jobs
            if job["attempt"].id != attempt_id
        )
        self._refresh_generation_tasks()
        self._pump_queue()

    def _split_source_task(
        self,
        task: ProjectTask,
        prompt: str,
    ) -> int:
        strategy = self.app_settings.auto_strategy()
        specs: list[GenerationPairSpec] = []
        groups: list[DressTaskGroup] = []
        provider_code = str(self.provider_combo.currentData() or "")
        provider_name = (
            self.provider_combo.currentText()
            if provider_code
            else ""
        )
        for group in self.pairs:
            group_prompt = (
                "无需制作"
                if group.skipped
                else self._prompt_for_group(prompt, group)
            )
            selected_model = self.model_combo.currentData()
            model_id = selected_model
            if selected_model == AUTO_MODEL_ID:
                model_id = choose_model(
                    prompt=group_prompt,
                    mode="dress_up",
                    reference_count=len(group.references),
                    strategy=strategy,
                ).model
            garment_hashes = tuple(
                image.content_hash
                for role, image in zip(group.roles, group.references)
                if role == "garment"
            )
            specs.append(
                GenerationPairSpec(
                    prompt_text=group_prompt,
                    model=model_id,
                    aspect_ratio=self.aspect_combo.currentData(),
                    resolution=self.resolution_combo.currentData(),
                    n=self.count_spin.value(),
                    unit_price=self.repository.current_model_price(
                        model_id
                    ),
                    garment_hashes=garment_hashes,
                    skipped=group.skipped,
                    requested_provider_code=provider_code,
                    requested_provider_name=provider_name,
                )
            )
            groups.append(group)
        unsupported_models = []
        if provider_code:
            for spec in specs:
                if spec.skipped:
                    continue
                routes = self.repository.list_provider_routes(
                    spec.model,
                    provider_code=provider_code,
                )
                if not routes:
                    unsupported_models.append(spec.model)
        if unsupported_models:
            QMessageBox.information(
                self,
                "首选供应商不支持当前模型",
                "以下模型未配置在该供应商中，将自动切换到其他平台：\n"
                + "、".join(sorted(set(unsupported_models))),
            )
        results = self.repository.split_and_queue_generations(
            task.id,
            self.actor,
            pairs=specs,
        )
        snapshot_failures: list[str] = []
        for result, group in zip(results, groups):
            if result.skipped or result.attempt_id is None:
                continue
            child = self.repository.get_task(result.task_id)
            if child is None:
                continue
            try:
                child_store = LocalTaskSourceStore(
                    self.file_store.paths.root / "local_sources",
                    self.actor.id,
                    child,
                    self.resolver,
                )
                garments = tuple(
                    image
                    for role, image in zip(group.roles, group.references)
                    if role == "garment"
                )
                child_store.replace_all(garments, group.person)
                child_store.save_attempt_snapshot(result.attempt_id)
            except Exception as exc:
                snapshot_failures.append(result.task_code)
                try:
                    self.repository.fail_generation_attempt(
                        result.attempt_id,
                        self.actor,
                        error_code="local_source_snapshot",
                        error_message=str(exc),
                    )
                except Exception:
                    pass
        if snapshot_failures:
            QMessageBox.warning(
                self,
                "部分素材保存失败",
                "以下子任务的本机素材快照保存失败：\n"
                + "、".join(snapshot_failures),
            )
        return len(results)

    def _retry_child_task(
        self,
        task: ProjectTask,
        prompt: str,
    ) -> int:
        active_pairs = [pair for pair in self.pairs if not pair.skipped]
        if len(self.pairs) != 1:
            raise ValueError(
                "失败子任务重试只能保留一组配对。"
            )
        if not active_pairs:
            self.repository.mark_task_skipped(task.id, self.actor)
            return 1
        group = active_pairs[0]
        selected_model = self.model_combo.currentData()
        model_id = selected_model
        if selected_model == AUTO_MODEL_ID:
            model_id = choose_model(
                prompt=prompt,
                mode="dress_up",
                reference_count=len(group.references),
                strategy=AUTO_STRATEGY_QUALITY,
            ).model
        attempt = self.repository.queue_generation_attempt(
            task.id,
            self.actor,
            prompt=prompt,
            model=model_id,
            aspect_ratio=self.aspect_combo.currentData(),
            resolution=self.resolution_combo.currentData(),
            n=self.count_spin.value(),
            unit_price=self.repository.current_model_price(model_id),
            requested_provider_code=str(
                self.provider_combo.currentData() or ""
            ),
            requested_provider_name=(
                self.provider_combo.currentText()
                if self.provider_combo.currentData()
                else ""
            ),
            allow_reopen=task.status in ("approved", "skipped"),
        )
        store = self.local_store
        if store is None:
            store = LocalTaskSourceStore(
                self.file_store.paths.root / "local_sources",
                self.actor.id,
                task,
                self.resolver,
            )
        garments = tuple(
            image
            for role, image in zip(group.roles, group.references)
            if role == "garment"
        )
        try:
            store.replace_all(garments, group.person)
            store.save_attempt_snapshot(attempt.id)
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
            raise
        return 1

    def _resume_queued_jobs(self) -> None:
        try:
            attempts = self.repository.list_queued_attempts(
                self.actor.id
            )
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
                {"task": task, "attempt": attempt}
            )

    def _pump_queue(self) -> None:
        if not hasattr(self, "active_workers"):
            return
        try:
            limit = self.repository.get_account_policy(
                self.actor.id
            )["max_generation_concurrency"]
        except Exception:
            limit = 2
        while self.waiting_jobs and len(self.active_workers) < limit:
            job = self.waiting_jobs.popleft()
            attempt: GenerationAttempt = job["attempt"]
            task: ProjectTask = job["task"]
            store = LocalTaskSourceStore(
                self.file_store.paths.root / "local_sources",
                self.actor.id,
                task,
                self.resolver,
            )
            local_references = store.load_attempt_snapshot(attempt.id)
            worker = ProjectGenerationWorker(
                repository=self.repository,
                client=self.client,
                resolver=self.resolver,
                task=task,
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
                lambda _task_id, _attempt_id, item=worker: (
                    self._job_finished(item)
                )
            )
            worker.signals.failed.connect(
                lambda _task_id, message, _attempt_id, item=worker: (
                    self._job_failed(item, message)
                )
            )
            self.active_workers.add(worker)
            worker.start()
        self._refresh_queue_state()

    def _job_finished(self, worker: ProjectGenerationWorker) -> None:
        self.active_workers.discard(worker)
        self._refresh_generation_tasks()
        self._pump_queue()

    def _job_failed(
        self,
        worker: ProjectGenerationWorker,
        message: str,
    ) -> None:
        self.active_workers.discard(worker)
        self._refresh_generation_tasks()
        self._pump_queue()
        QMessageBox.warning(self, "生成失败", message)

    def _refresh_queue_state(self) -> None:
        if not hasattr(self, "feedback_label"):
            return
        self.feedback_label.setText(
            f"正在生成 {len(self.active_workers)} 个，"
            f"等待 {len(self.waiting_jobs)} 个。"
        )

    def is_generating(self) -> bool:
        return bool(self.active_workers or self.waiting_jobs)
