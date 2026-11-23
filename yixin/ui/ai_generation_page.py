from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..api import NanoBananaClient
from ..collaboration.local_sources import (
    LocalTaskSourceStore,
    ProjectGarmentStore,
)
from ..collaboration.pending_results import (
    clear_pending_results,
    load_pending_results,
)
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    ATTEMPT_SUCCEEDED,
    PROJECT_STATUS_LABELS,
    TASK_AWAITING_ACCEPTANCE,
    TASK_APPROVED,
    TASK_SKIPPED,
    TASK_STATUS_LABELS,
    AuthUser,
    GenerationAttempt,
    GenerationPairSpec,
    ProjectRecord,
    ProjectTask,
)
from ..collaboration.worker import ProjectGenerationWorker
from ..constants import (
    ASPECT_RATIOS,
    AUTO_MODEL_ID,
    BUILTIN_PROMPT_TEMPLATES,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MODEL,
    DEFAULT_RESOLUTION,
    MODEL_LABELS,
    MODEL_SELECTION_OPTIONS,
    RESOLUTIONS,
)
from ..database import Database
from ..library import LibraryService
from ..model_router import choose_model
from ..paging import PageRequest, TaskQuery
from ..security import SecretStore
from ..settings import AUTO_STRATEGY_QUALITY
from ..storage import FileStore, StoredImage
from ..worker import TaskWorker
from .batch_dress_page import (
    BatchDressPage,
    DressTaskGroup,
)
from .async_queries import get_query_runner


class AIGenerationPage(BatchDressPage):
    tasks_skipped = Signal(int)

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
        async_mode: bool = False,
    ) -> None:
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.client = client
        self.key_store = key_store
        self.generation_tasks: list[ProjectTask] = []
        self._generation_tasks_by_id: dict[int, ProjectTask] = {}
        self.generation_projects: list[ProjectRecord] = []
        self._generation_projects_by_id: dict[int, ProjectRecord] = {}
        self.current_task: ProjectTask | None = None
        self.current_project: ProjectRecord | None = None
        self.local_store: LocalTaskSourceStore | None = None
        self.active_workers: set[ProjectGenerationWorker] = set()
        self.waiting_jobs: deque[dict] = deque()
        self.api_providers = []
        self.project_progress: dict[int, tuple[int, int]] = {}
        self.force_direct_retry_task_id: int | None = None
        self._loading_source_state = False
        self._prompt_cache: dict[int, str] = {}
        self._task_settings: dict[int, dict] = {}
        self._project_settings: dict[int, dict] = {}
        self._project_garment_stores: dict[int, ProjectGarmentStore] = {}
        self.async_mode = async_mode
        self.query_runner = (
            get_query_runner() if async_mode else None
        )
        self._active_task_request = -1
        super().__init__(
            database,
            file_store,
            library_service,
            worker,
            secret_store,
            parent,
            allow_pair_skip=False,
            allow_bottom_skip=False,
            generate_button_text="加入队列",
            auto_load=False,
            async_thumbnails=async_mode,
            repository=repository,
        )
        self.count_spin.setRange(1, 1)
        self.count_spin.setValue(1)
        self.count_spin.hide()
        self.count_label.hide()
        self.skip_project_button = QPushButton("本主任务不做图")
        self.skip_project_button.setMinimumHeight(42)
        self.skip_project_button.setEnabled(False)
        self.skip_project_button.clicked.connect(
            self._skip_project_tasks
        )
        action_bar = self.generate_button.parentWidget().layout
        action_bar.insertWidget(
            action_bar.indexOf(self.generate_button),
            self.skip_project_button,
        )
        self.task_selector.currentIndexChanged.connect(
            self._selection_changed
        )
        self.project_filter_combo.currentIndexChanged.connect(
            self._project_filter_changed
        )
        self.task_refresh_button.clicked.connect(self.refresh)
        self._task_search_timer = QTimer(self)
        self._task_search_timer.setSingleShot(True)
        self._task_search_timer.setInterval(350)
        self._task_search_timer.timeout.connect(
            self._request_generation_tasks
        )
        self.task_search_edit.textChanged.connect(
            lambda: self._task_search_timer.start()
        )
        if self.query_runner is not None:
            self.query_runner.succeeded.connect(self._query_succeeded)
            self.query_runner.failed.connect(self._query_failed)
        self._disposed = False
        self.destroyed.connect(self._dispose_query_runner)
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
        layout.setSpacing(8)
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("项目"))
        self.project_filter_combo = QComboBox()
        self.project_filter_combo.setMinimumWidth(150)
        self.project_filter_combo.addItem("全部项目", None)
        title_row.addWidget(self.project_filter_combo)
        title_row.addSpacing(8)
        title_row.addWidget(QLabel("主任务"))
        self.task_search_edit = QLineEdit()
        self.task_search_edit.setPlaceholderText("搜索主任务、货号或项目")
        self.task_search_edit.setFixedWidth(180)
        title_row.addWidget(self.task_search_edit)
        self.task_selector = QComboBox()
        self.task_selector.setMinimumWidth(420)
        title_row.addWidget(self.task_selector)
        self.task_refresh_button = QPushButton("刷新")
        self.task_refresh_button.setObjectName("SecondaryButton")
        title_row.addWidget(self.task_refresh_button)
        self.restore_all_button = QPushButton("恢复全部素材")
        self.restore_all_button.setEnabled(False)
        self.restore_all_button.clicked.connect(self._restore_all_sources)
        title_row.addWidget(self.restore_all_button)
        self.task_status_label = QLabel("尚未选择")
        self.task_status_label.setStyleSheet("color:#60708a;")
        title_row.addWidget(self.task_status_label)
        layout.addLayout(title_row)
        return card

    def _additional_settings_rows(self, _form: QFormLayout) -> None:
        return

    def _build_settings(self) -> QWidget:
        card = QGroupBox("生成设置")
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        row = QHBoxLayout()
        self.model_combo = QComboBox()
        for name, model_id in MODEL_SELECTION_OPTIONS:
            self.model_combo.addItem(name, model_id)
        self.model_combo.setCurrentIndex(
            next(
                index
                for index, (_, model_id) in enumerate(
                    MODEL_SELECTION_OPTIONS
                )
                if model_id == DEFAULT_MODEL
            )
        )
        self.aspect_combo = QComboBox()
        for value in ASPECT_RATIOS:
            self.aspect_combo.addItem(value, value)
        self.aspect_combo.setCurrentText(DEFAULT_ASPECT_RATIO)
        self.resolution_combo = QComboBox()
        for value in RESOLUTIONS:
            self.resolution_combo.addItem(
                "自动（不发送）" if value == "auto" else value,
                value,
            )
        self.resolution_combo.setCurrentIndex(
            next(
                index
                for index, value in enumerate(RESOLUTIONS)
                if value == DEFAULT_RESOLUTION
            )
        )
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 4)
        self.count_spin.setValue(DEFAULT_BATCH_SIZE)
        self.count_label = QLabel("每对数量")
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("自动选择", "")
        row.addWidget(QLabel("模型"))
        row.addWidget(self.model_combo)
        row.addSpacing(12)
        row.addWidget(QLabel("参数"))
        row.addWidget(QLabel("宽高比"))
        row.addWidget(self.aspect_combo)
        row.addWidget(QLabel("分辨率"))
        row.addWidget(self.resolution_combo)
        row.addWidget(self.count_label)
        row.addWidget(self.count_spin)
        row.addSpacing(12)
        row.addWidget(QLabel("供应商"))
        row.addWidget(self.provider_combo)
        row.addSpacing(12)
        row.addWidget(QLabel("提示词模板"))
        self._build_template_controls(row)
        row.addStretch(1)
        layout.addLayout(row)
        self.note_label = QLabel("备注：-")
        self.note_label.setWordWrap(True)
        self._apply_note_style(False)
        layout.addWidget(self.note_label)
        return card

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
        self.prompt_edit.textChanged.connect(self._cache_current_prompt)
        layout.addWidget(self.prompt_edit)
        return card

    def _default_prompt(self) -> str:
        for name, prompt in BUILTIN_PROMPT_TEMPLATES:
            if "基础服装替换" in name:
                return prompt
        return BUILTIN_PROMPT_TEMPLATES[0][1]

    def _select_default_template(self) -> str | None:
        """把模板下拉切到“基础服装替换”，返回该模板提示词（没有则 None）。"""
        if not hasattr(self, "template_combo"):
            return None
        for index in range(self.template_combo.count()):
            data = self.template_combo.itemData(index)
            if data and "基础服装替换" in str(data.get("name") or ""):
                self.template_combo.blockSignals(True)
                self.template_combo.setCurrentIndex(index)
                self.template_combo.blockSignals(False)
                return str(data.get("prompt") or "") or None
        return None

    def _cache_current_prompt(self) -> None:
        task = self.current_task
        if task is not None:
            self._prompt_cache[task.id] = self.prompt_edit.toPlainText()

    def _current_settings(self) -> dict:
        provider_code = (
            self.provider_combo.currentData()
            if hasattr(self, "provider_combo")
            else ""
        )
        return {
            "prompt": self.prompt_edit.toPlainText(),
            "model": self.model_combo.currentData(),
            "aspect": self.aspect_combo.currentData(),
            "resolution": self.resolution_combo.currentData(),
            "provider": provider_code,
            "provider_name": (
                self.provider_combo.currentText()
                if provider_code
                else ""
            ),
        }

    def _snapshot_current(self) -> None:
        settings = self._current_settings()
        project = self.current_project
        if project is not None:
            self._project_settings[project.id] = settings
            return
        task = self.current_task
        if task is None:
            return
        self._task_settings[task.id] = settings
        self._project_settings[task.project_id] = settings

    def _refresh_providers(self) -> None:
        try:
            providers = self.repository.list_api_providers(
                include_disabled=False
            )
        except Exception:
            providers = []
        self._apply_providers(providers)

    def _apply_providers(self, providers) -> None:
        selected = self.provider_combo.currentData()
        self.api_providers = list(providers)
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
        try:
            shared_root = self.repository.get_shared_setting("share_root")
        except Exception:
            shared_root = None
        if shared_root:
            self.resolver = SharedPathResolver(shared_root)
        if not self.async_mode:
            self._refresh_providers()
        self.refresh_templates()
        self._refresh_generation_project_filter()
        if self.async_mode:
            self._request_generation_tasks()
        else:
            self._refresh_generation_tasks()
        self._resume_queued_jobs()
        self._pump_queue()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._resume_queued_jobs()
        self._pump_queue()

    def _project_filter_changed(self, _index: int) -> None:
        self._refresh_generation_tasks()

    def _refresh_generation_project_filter(self) -> None:
        current = self.project_filter_combo.currentData()
        names: list[str] = []
        try:
            names = list(
                self.repository.list_access_project_names(self.actor)
            )
        except Exception:
            names = []
        if not names:
            try:
                names = [
                    name
                    for _scope, name in (
                        self.repository.list_accessible_projects(
                            self.actor
                        )
                    )
                ]
            except Exception:
                names = []
        if not names:
            names = sorted(
                {
                    project.project_name
                    for project in self.generation_projects
                    if project.project_name
                }
            )
        self.project_filter_combo.blockSignals(True)
        self.project_filter_combo.clear()
        self.project_filter_combo.addItem("全部项目", None)
        for name in names:
            self.project_filter_combo.addItem(name, name)
        index = self.project_filter_combo.findData(current)
        self.project_filter_combo.setCurrentIndex(max(0, index))
        self.project_filter_combo.blockSignals(False)

    def _generation_query(self) -> TaskQuery:
        return TaskQuery(
            keyword=self.task_search_edit.text().strip(),
            project_name=self.project_filter_combo.currentData(),
        )

    def _refresh_generation_tasks(self) -> None:
        if self.async_mode:
            self._request_generation_tasks()
            return
        try:
            result = self.repository.list_ai_generation_projects_page(
                self.actor,
                self._generation_query(),
                PageRequest(page=1, page_size=10000),
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取AI生图任务失败", str(exc))
            return
        self._render_generation_projects(list(result.items))

    def _request_generation_tasks(self) -> None:
        page = PageRequest(page=1, page_size=10000)
        query = self._generation_query()
        self.task_status_label.setText("正在加载主任务…")
        self.task_selector.setEnabled(False)
        self._update_project_batch_state()

        def load():
            result = self.repository.list_ai_generation_projects_page(
                self.actor,
                query,
                page,
            )
            try:
                providers = list(
                    self.repository.list_api_providers(
                        include_disabled=False
                    )
                )
            except Exception:
                providers = list(self.api_providers)
            return result.items, providers

        self._active_task_request = self.query_runner.submit(load)

    def _dispose_query_runner(self, *_args) -> None:
        self._disposed = True
        if self.query_runner is None:
            return
        for signal, slot in (
            (self.query_runner.succeeded, self._query_succeeded),
            (self.query_runner.failed, self._query_failed),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def _query_succeeded(self, request_id: int, result) -> None:
        if getattr(self, "_disposed", False):
            return
        if not self.async_mode or request_id != self._active_task_request:
            return
        projects, providers = result
        self._apply_providers(providers)
        self._render_generation_projects(projects)

    def _query_failed(self, request_id: int, message: str) -> None:
        if getattr(self, "_disposed", False):
            return
        if not self.async_mode or request_id != self._active_task_request:
            return
        self.task_selector.setEnabled(True)
        self.task_status_label.setText("主任务加载失败")
        self._update_project_batch_state()
        QMessageBox.warning(self, "读取AI生图任务失败", message)

    def _find_selector_data(self, data) -> int:
        for index in range(self.task_selector.count()):
            if self.task_selector.itemData(index) == data:
                return index
        return -1

    def _render_generation_projects(
        self,
        projects: list[ProjectRecord],
    ) -> None:
        current = self.task_selector.currentData()
        forced_id = self.force_direct_retry_task_id
        self.generation_projects = list(projects)
        self._generation_projects_by_id = {
            project.id: project for project in projects
        }
        self.project_progress = {
            project.id: (project.approved_count, project.task_count)
            for project in projects
        }
        self.task_selector.blockSignals(True)
        self.task_selector.clear()
        self.task_selector.addItem("请选择主任务", None)
        for project in projects:
            urgent_prefix = "【加急】" if project.is_urgent else ""
            label = (
                f"{urgent_prefix}{project.project_code} · "
                f"{project.product_code or project.name}"
                f" · 子任务 {project.approved_count}/{project.task_count}"
                f" · {PROJECT_STATUS_LABELS.get(project.status, project.status)}"
            )
            self.task_selector.addItem(
                label,
                ("project", project.id),
            )
        if forced_id is not None:
            task = self._generation_tasks_by_id.get(forced_id)
            if task is not None:
                self.task_selector.addItem(
                    f"{task.task_code} · 历史重试",
                    ("retry", forced_id),
                )
        target = (
            self._find_selector_data(current)
            if current is not None
            else (1 if (projects or forced_id is not None) else 0)
        )
        self.task_selector.setCurrentIndex(max(0, target))
        self.task_selector.setEnabled(True)
        self.task_selector.blockSignals(False)
        if target <= 0:
            self._clear_generation_editor()
        else:
            self._selection_changed()

    def _apply_note_style(self, has_note: bool) -> None:
        if has_note:
            self.note_label.setStyleSheet(
                "color:#8a4b00;background:#fff4d6;"
                "border:1px solid #f0c36d;border-radius:6px;"
                "padding:6px 10px;font-weight:600;"
            )
        else:
            self.note_label.setStyleSheet(
                "color:#60708a;background:#f4f6fa;"
                "border:1px solid #e3e9f2;border-radius:6px;"
                "padding:6px 10px;"
            )

    def _set_note(self, note: str) -> None:
        if not hasattr(self, "note_label"):
            return
        text = (note or "").strip()
        self.note_label.setText(f"备注：{text}" if text else "备注：无")
        self._apply_note_style(bool(text))

    def _clear_generation_editor(self, *, persist: bool = True) -> None:
        self._snapshot_current()
        if persist:
            # 切换或清空选择前保存当前主任务/子任务的本机素材，
            # 避免新增、裁剪的图片随界面清空而丢失。
            self._persist_current_sources()
        self.current_task = None
        self.current_project = None
        self.local_store = None
        self.restore_all_button.setEnabled(False)
        self.garment_assets = []
        self.person_assets = []
        self.pairs = []
        self._refresh_role("garment")
        self._refresh_role("person")
        self._refresh_pairs()
        self.task_status_label.setText("没有可制作的主任务")
        self.task_status_label.setStyleSheet("color:#60708a;")
        self._set_note("")
        self._update_asset_actions()
        self._update_project_batch_state()

    def _update_project_batch_state(self) -> None:
        self._update_skip_buttons()

    def _update_skip_buttons(self) -> None:
        project = self.current_project
        task = self.current_task
        enabled = False
        if project is not None:
            enabled = self.task_selector.isEnabled()
        elif task is not None:
            enabled = self.force_direct_retry_task_id != task.id
        self.skip_project_button.setEnabled(enabled)

    def _selection_changed(self, _index: int | None = None) -> None:
        data = self.task_selector.currentData()
        if isinstance(data, tuple) and data[0] == "retry":
            self._retry_task_changed(int(data[1]))
        elif isinstance(data, tuple) and data[0] == "project":
            project = self._generation_projects_by_id.get(int(data[1]))
            if project is None:
                self._clear_generation_editor()
            else:
                self._project_changed(project)
        else:
            self._clear_generation_editor()

    def _project_changed(self, project: ProjectRecord) -> None:
        self._snapshot_current()
        self._persist_current_sources()
        self.current_project = project
        self.current_task = None
        self.local_store = None
        store = self._project_garment_store(project.id)
        cached = store.load_all(project.source_revision)
        if cached is None:
            try:
                assets = self.repository.list_project_assets(project.id)
            except Exception as exc:
                QMessageBox.warning(self, "读取主任务素材失败", str(exc))
                self._clear_generation_editor(persist=False)
                return
            garments = tuple(
                self._asset_image(project, asset)
                for asset in assets
                if asset.role == "garment"
            )
            models = tuple(
                self._asset_image(project, asset)
                for asset in assets
                if asset.role == "model"
            )
        else:
            garments, models = cached
        self.garment_assets = list(garments)
        self.person_assets = list(models)
        self.restore_all_button.setEnabled(True)
        self.selected_garment = None
        self.pairs = []
        self._loading_source_state = True
        try:
            self._refresh_role("garment")
            self._refresh_role("person")
        finally:
            self._loading_source_state = False
        self._refresh_pairs()
        self._auto_match()
        settings = self._project_settings.get(project.id) or {}
        prompt = str(settings.get("prompt") or "")
        if not prompt:
            prompt = (
                self._select_default_template()
                or self._default_prompt()
            )
        self.prompt_edit.setPlainText(prompt)
        model_id = settings.get("model") or DEFAULT_MODEL
        index = self.model_combo.findData(model_id)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        aspect_value = (
            settings.get("aspect") or DEFAULT_ASPECT_RATIO
        )
        index = self.aspect_combo.findData(aspect_value)
        if index >= 0:
            self.aspect_combo.setCurrentIndex(index)
        resolution_value = (
            settings.get("resolution") or DEFAULT_RESOLUTION
        )
        index = self.resolution_combo.findData(resolution_value)
        if index >= 0:
            self.resolution_combo.setCurrentIndex(index)
        provider_code = settings.get("provider") or ""
        index = self.provider_combo.findData(provider_code)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)
        urgent_text = " · 加急款" if project.is_urgent else ""
        self.task_status_label.setText(
            f"{PROJECT_STATUS_LABELS.get(project.status, project.status)}"
            f" · 子任务 {project.approved_count}/{project.task_count}"
            f" · 服装 {len(self.garment_assets)}"
            f" · 模特 {len(self.person_assets)}"
            f"{urgent_text}"
        )
        self.task_status_label.setStyleSheet(
            "color:#b72f3c;font-weight:700;"
            if project.is_urgent
            else "color:#60708a;"
        )
        self._set_note(project.note)
        self._update_asset_actions()
        self._update_project_batch_state()

    def _asset_image(self, project: ProjectRecord, asset):
        path = self.resolver.resolve_project_path(
            project.project_relative_root,
            asset.relative_path,
        )
        return StoredImage(
            path=path,
            content_hash=asset.content_hash,
            mime=asset.mime,
            width=asset.width,
            height=asset.height,
            original_name=asset.original_name,
            relative_path=asset.relative_path,
        )

    def _retry_task_changed(self, task_id: int) -> None:
        self._snapshot_current()
        self._persist_current_sources()
        task = self._generation_tasks_by_id.get(task_id)
        self.current_project = None
        self.task_status_label.setStyleSheet("color:#60708a;")
        self._set_note("")
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
            self._clear_generation_editor(persist=False)
            return
        self.restore_all_button.setEnabled(True)
        # 子任务重试只使用该子任务自己的素材，不能混入主任务全部素材，
        # 否则一次重试会生成多组配对，也会覆盖主任务级缓存。
        self.garment_assets = list(state.garments)
        self.person_assets = [state.model] if state.model else []
        self.selected_garment = None
        self.pairs = []
        self._loading_source_state = True
        try:
            self._refresh_role("garment")
            self._refresh_role("person")
        finally:
            self._loading_source_state = False
        self._refresh_pairs()
        self._auto_match()
        settings = (
            self._task_settings.get(task.id)
            or self._project_settings.get(task.project_id)
            or {}
        )
        prompt = self._prompt_cache.get(task.id)
        if prompt is None:
            prompt = str(settings.get("prompt") or "")
        if not prompt:
            prompt = task.prompt_text.strip()
        if not prompt:
            prompt = (
                self._select_default_template()
                or self._default_prompt()
            )
        self.prompt_edit.setPlainText(prompt)
        model_id = settings.get("model") or task.model
        if model_id:
            index = self.model_combo.findData(model_id)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
        aspect_value = settings.get("aspect") or task.aspect_ratio
        aspect_index = self.aspect_combo.findData(aspect_value)
        if aspect_index >= 0:
            self.aspect_combo.setCurrentIndex(aspect_index)
        resolution_value = (
            settings.get("resolution") or task.resolution or "auto"
        )
        resolution_index = self.resolution_combo.findData(
            resolution_value
        )
        if resolution_index >= 0:
            self.resolution_combo.setCurrentIndex(resolution_index)
        provider_code = settings.get("provider")
        if provider_code:
            provider_index = self.provider_combo.findData(provider_code)
            if provider_index >= 0:
                self.provider_combo.setCurrentIndex(provider_index)
        self.count_spin.setValue(max(1, int(task.n)))
        self.task_status_label.setText(
            f"主任务 "
            f"{self.project_progress.get(task.project_id, (0, 0))[0]}/"
            f"{self.project_progress.get(task.project_id, (0, 0))[1]}"
            f" · {TASK_STATUS_LABELS.get(task.status, task.status)}"
        )
        self._update_asset_actions()
        self._update_project_batch_state()

    def _refresh_task_status_label(self) -> None:
        task = self.current_task
        if task is None:
            self.task_status_label.setText("尚未选择")
            return
        completed, total = self.project_progress.get(task.project_id, (0, 0))
        self.task_status_label.setText(
            f"主任务 {completed}/{total} · "
            f"{TASK_STATUS_LABELS.get(task.status, task.status)}"
        )

    def _project_garment_store(
        self,
        project_id: int,
    ) -> ProjectGarmentStore:
        store = self._project_garment_stores.get(project_id)
        if store is None:
            store = ProjectGarmentStore(
                self.file_store.paths.root / "local_sources",
                self.actor.id,
                project_id,
            )
            self._project_garment_stores[project_id] = store
        return store

    def _persist_current_sources(self) -> None:
        project = self.current_project
        if project is not None:
            try:
                self._project_garment_store(project.id).save_all(
                    tuple(self.garment_assets),
                    tuple(self.person_assets),
                    project.source_revision,
                )
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "保存本机素材失败",
                    str(exc),
                )
            return
        if (
            self.current_task is None
            or self.local_store is None
        ):
            return
        try:
            self.local_store.replace_all(
                tuple(self.garment_assets),
                self.person_assets[0] if len(self.person_assets) == 1 else None,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "保存本机素材失败",
                str(exc),
            )

    def _refresh_role(self, role: str) -> None:
        super()._refresh_role(role)
        if not self._loading_source_state and self.local_store is not None:
            self._persist_current_sources()

    def _restore_all_sources(self) -> None:
        project = self.current_project
        if project is not None:
            try:
                assets = self.repository.list_project_assets(project.id)
                self.garment_assets = [
                    self._asset_image(project, asset)
                    for asset in assets
                    if asset.role == "garment"
                ]
                self.person_assets = [
                    self._asset_image(project, asset)
                    for asset in assets
                    if asset.role == "model"
                ]
                self._project_garment_store(project.id).save_all(
                    tuple(self.garment_assets),
                    tuple(self.person_assets),
                    project.source_revision,
                )
            except Exception as exc:
                QMessageBox.warning(self, "恢复失败", str(exc))
                return
            self._loading_source_state = True
            try:
                self._refresh_role("garment")
                self._refresh_role("person")
            finally:
                self._loading_source_state = False
            self.pairs = []
            self.selected_pair_key = None
            self._refresh_pairs()
            self._auto_match()
            self.feedback_label.setText("已恢复全部初始共享盘素材。")
            return
        if self.local_store is None:
            return
        try:
            state = self.local_store.restore_all()
        except Exception as exc:
            QMessageBox.warning(self, "恢复失败", str(exc))
            return
        self._loading_source_state = True
        try:
            self.garment_assets = list(state.garments)
            self.person_assets = [state.model] if state.model else []
            self.pairs = []
            self.selected_pair_key = None
            self._refresh_role("garment")
            self._refresh_role("person")
        finally:
            self._loading_source_state = False
        self._refresh_pairs()
        self._auto_match()
        self.feedback_label.setText("已恢复全部初始共享盘素材。")

    def _enqueue_all(self) -> None:
        task = self.current_task
        project = self.current_project
        retry_mode = (
            task is not None
            and self.force_direct_retry_task_id == task.id
        )
        if not retry_mode and project is None:
            QMessageBox.information(
                self,
                "未选择主任务",
                "请先在顶部选择需要制作的主任务。",
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
        if active_pairs and not self._check_api_key_ready():
            return
        try:
            if retry_mode:
                count = self._retry_child_task(task, prompt)
            else:
                specs = self._build_pair_specs(
                    self.pairs,
                    prompt,
                    self._current_settings(),
                )
                results = self.repository.split_project_and_queue_generations(
                    project.id,
                    self.actor,
                    pairs=specs,
                )
                self._save_project_child_snapshots(results, self.pairs)
                count = len(results)
        except Exception as exc:
            QMessageBox.warning(self, "加入队列失败", str(exc))
            return
        if count == 0:
            self.feedback_label.setText(
                "没有新的配对需要提交，可能已在队列或已完成。"
            )
            return
        self.pairs.clear()
        self._refresh_pairs()
        self.tasks_enqueued.emit(count)
        self.feedback_label.setText(
            f"已提交 {count} 个配对任务。"
        )
        self._refresh_generation_tasks()
        self._resume_queued_jobs()
        self._pump_queue()
        self.force_direct_retry_task_id = None

    def _build_pair_specs(
        self,
        groups: Sequence[DressTaskGroup],
        prompt: str,
        settings: dict,
    ) -> list[GenerationPairSpec]:
        strategy = self.app_settings.auto_strategy()
        specs: list[GenerationPairSpec] = []
        selected_model = settings.get("model")
        aspect_ratio = settings.get("aspect")
        resolution = settings.get("resolution")
        provider_code = str(settings.get("provider") or "")
        provider_name = str(settings.get("provider_name") or "")
        for group in groups:
            group_prompt = (
                "无需制作"
                if group.skipped
                else self._prompt_for_group(prompt, group)
            )
            model_id = selected_model
            if selected_model == AUTO_MODEL_ID:
                model_id = choose_model(
                    prompt=group_prompt,
                    mode="dress_up",
                    reference_count=len(group.references),
                    strategy=strategy,
                ).model
            if not group.skipped:
                usable_model = self._resolve_usable_model(
                    model_id,
                    len(group.references),
                    1,
                )
                if usable_model is None:
                    raise ValueError(
                        "当前模型在已启用平台中没有可用的 API Key，"
                        "请改用其他模型"
                    )
                model_id = usable_model
            garment_hashes = tuple(
                image.content_hash
                for role, image in zip(group.roles, group.references)
                if role == "garment"
            )
            specs.append(
                GenerationPairSpec(
                    prompt_text=group_prompt,
                    model=model_id,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    n=1,
                    unit_price=self.repository.current_model_price(
                        model_id
                    ),
                    garment_hashes=garment_hashes,
                    person_hash=(
                        group.person.content_hash
                        if group.person is not None
                        else ""
                    ),
                    skipped=group.skipped,
                    requested_provider_code=provider_code,
                    requested_provider_name=provider_name,
                )
            )
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
        return specs

    def _save_project_child_snapshots(
        self,
        results,
        groups: Sequence[DressTaskGroup],
    ) -> None:
        snapshot_failures: list[str] = []
        for result in results:
            if result.skipped or result.attempt_id is None:
                continue
            child = self.repository.get_task(result.task_id)
            if child is None:
                continue
            group = (
                groups[result.pair_index]
                if 0 <= result.pair_index < len(groups)
                else None
            )
            try:
                child_store = LocalTaskSourceStore(
                    self.file_store.paths.root / "local_sources",
                    self.actor.id,
                    child,
                    self.resolver,
                )
                # 轮次快照必须使用页面上实际发送的素材，
                # 这样裁剪/新增的图片才能进入生成和历史记录。
                if group is not None:
                    garments = tuple(
                        image
                        for role, image in zip(
                            group.roles,
                            group.references,
                        )
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

    def _check_api_key_ready(self) -> bool:
        try:
            if not self.key_store.load():
                raise ValueError(
                    "当前账号尚未配置 API Key，请联系管理员"
                )
        except Exception as exc:
            QMessageBox.warning(self, "API Key 不可用", str(exc))
            return False
        return True

    def _reset_queue_after_skip(self) -> None:
        self.waiting_jobs.clear()
        self._resume_queued_jobs()
        self._pump_queue()

    def _skip_project_tasks(self) -> None:
        project = self.current_project
        task = self.current_task
        project_id = (
            project.id
            if project is not None
            else (task.project_id if task is not None else None)
        )
        if project_id is None:
            QMessageBox.information(
                self,
                "未选择主任务",
                "请先在顶部选择需要制作的主任务。",
            )
            return
        answer = QMessageBox.question(
            self,
            "本主任务不做图",
            "本主任务下所有尚未开始、尚未完成的子任务将标记为"
            "“无需制作”，生成中的任务不受影响。\n确定继续吗？",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            count = self.repository.skip_project(
                project_id,
                self.actor,
            )
        except Exception as exc:
            QMessageBox.warning(self, "跳过失败", str(exc))
            return
        self.feedback_label.setText(
            f"本主任务已标记无需制作（{count} 个子任务）。"
        )
        self.tasks_skipped.emit(count)
        self._refresh_generation_tasks()
        self._reset_queue_after_skip()

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
        self.force_direct_retry_task_id = task.id
        self._generation_tasks_by_id[task.id] = task
        self.generation_tasks = [
            item for item in self.generation_tasks if item.id != task.id
        ] + [task]
        retry_data = ("retry", task.id)
        index = self._find_selector_data(retry_data)
        if index < 0:
            self.task_selector.addItem(
                f"{task.task_code} · 历史重试",
                retry_data,
            )
            index = self.task_selector.count() - 1
        self.task_selector.setCurrentIndex(index)
        self._retry_task_changed(task.id)
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
        self.count_spin.setValue(1)
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
        pending_attempts = [
            item.id
            for item in self.repository.list_attempts(task.id)
            if item.status == ATTEMPT_SUCCEEDED
            and load_pending_results(
                self.file_store.paths.temp,
                item.id,
            )
        ]
        if pending_attempts:
            answer = QMessageBox.question(
                self,
                "丢弃待确认结果",
                "该子任务还有未确认的生成结果。\n"
                "重试会丢弃这些结果，确定继续吗？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.feedback_label.setText(
                    "已取消重试，待确认结果未受影响。"
                )
                return 0
        selected_model = self.model_combo.currentData()
        model_id = selected_model
        if selected_model == AUTO_MODEL_ID:
            model_id = choose_model(
                prompt=prompt,
                mode="dress_up",
                reference_count=len(group.references),
                strategy=AUTO_STRATEGY_QUALITY,
            ).model
        usable_model = self._resolve_usable_model(
            model_id,
            len(group.references),
            1,
        )
        if usable_model is None:
            raise ValueError(
                "当前模型在已启用平台中没有可用的 API Key，"
                "请改用其他模型"
            )
        model_id = usable_model
        attempt = self.repository.queue_generation_attempt(
            task.id,
            self.actor,
            prompt=prompt,
            model=model_id,
            aspect_ratio=self.aspect_combo.currentData(),
            resolution=self.resolution_combo.currentData(),
            n=1,
            unit_price=self.repository.current_model_price(model_id),
            requested_provider_code=str(
                self.provider_combo.currentData() or ""
            ),
            requested_provider_name=(
                self.provider_combo.currentText()
                if self.provider_combo.currentData()
                else ""
            ),
            allow_reopen=task.status
            in (TASK_APPROVED, TASK_SKIPPED, TASK_AWAITING_ACCEPTANCE),
        )
        for attempt_id in pending_attempts:
            clear_pending_results(self.file_store.paths.temp, attempt_id)
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
                pending_root=self.file_store.paths.temp,
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
            worker.signals.deferred.connect(
                lambda _task_id, _attempt_id, item=worker: (
                    self._job_deferred(item)
                )
            )
            self.active_workers.add(worker)
            worker.start()
        self._refresh_queue_state()

    def _job_finished(self, worker: ProjectGenerationWorker) -> None:
        self.active_workers.discard(worker)
        self._refresh_generation_tasks()
        self._pump_queue()

    def _job_deferred(self, worker: ProjectGenerationWorker) -> None:
        # 账号并发名额已满，轮次仍保持排队，稍后自动重试。
        self.active_workers.discard(worker)
        self.waiting_jobs.appendleft(
            {
                "task": worker.task,
                "attempt": worker.reserved_attempt,
            }
        )
        self._refresh_queue_state()
        QTimer.singleShot(5000, self._pump_queue)

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
