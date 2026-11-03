from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QApplication,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..api import NanoBananaClient
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.shared_paths import SharedPathResolver
from ..collaboration.types import (
    ROLE_ADMIN,
    ROLE_AI,
    ROLE_OPERATIONS,
    ROLE_LABELS,
    ROLE_DEFAULT_MENUS,
    ROLE_ALLOWED_MENUS,
    AuthUser,
)
from ..constants import APP_NAME, MODE_DRESS_UP, MODE_FREE_COMPOSE, STATUS_LABELS
from ..database import Database
from ..library import LibraryService
from ..paths import AppPaths
from ..security import SecretStore
from ..storage import FileStore
from ..worker import TaskWorker
from .batch_dress_page import BatchDressPage
from .admin_page import AdminPage
from .create_page import CreatePage
from .dialogs import SettingsDialog
from .unified_history_page import UnifiedHistoryPage
from .library_page import LibraryPage
from .submission_list_page import SubmissionListPage
from .ai_generation_page import AIGenerationPage
from .task_assignment_page import TaskAssignmentPage
from .task_create_page import TaskCreatePage


class NavItem:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class ElidedLabel(QLabel):
    def __init__(
        self,
        text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._full_text = text
        self.setToolTip(text)
        self.setMaximumWidth(180)
        self.setMinimumWidth(80)
        self.setWordWrap(False)
        self._refresh_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_text()

    def _refresh_text(self) -> None:
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                max(1, self.width()),
            )
        )


class TopNavigation(QWidget):
    currentRowChanged = Signal(int)
    settings_requested = Signal()

    def __init__(
        self,
        labels: list[str],
        parent: QWidget | None = None,
        *,
        display_name: str = "",
        role_label: str = "",
        settings_visible: bool = False,
    ) -> None:
        super().__init__(parent)
        self._current_row = -1
        self._buttons: list[QPushButton] = []
        self._items = [NavItem(text) for text in labels]
        self._page_group = QButtonGroup(self)
        self._page_group.setExclusive(True)

        header = QFrame()
        header.setObjectName("TopHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 8, 18, 8)
        header_layout.setSpacing(6)
        brand_box = QVBoxLayout()
        brand_box.setSpacing(0)
        brand = QLabel("亦欣 AI")
        brand.setObjectName("TopBrandTitle")
        brand_subtitle = QLabel("AI 视觉生产工作台")
        brand_subtitle.setObjectName("TopBrandSubtitle")
        brand_box.addWidget(brand)
        brand_box.addWidget(brand_subtitle)
        header_layout.addLayout(brand_box)
        for index, text in enumerate(labels):
            button = QPushButton(text)
            button.setObjectName("TopCategory")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, row=index: self.setCurrentRow(row)
            )
            button.setToolTip(text)
            self._page_group.addButton(button, index)
            self._buttons.append(button)
            header_layout.addWidget(button)
        header_layout.addStretch(1)
        user_box = QVBoxLayout()
        user_box.setSpacing(0)
        user = ElidedLabel(display_name)
        user.setObjectName("TopUserLabel")
        role = QLabel(role_label)
        role.setObjectName("TopUserLabel")
        user_box.addWidget(user)
        user_box.addWidget(role)
        header_layout.addLayout(user_box)
        self.settings_button = QPushButton("设置")
        self.settings_button.setVisible(settings_visible)
        self.settings_button.clicked.connect(
            self.settings_requested.emit
        )
        header_layout.addWidget(self.settings_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)

    def count(self) -> int:
        return len(self._items)

    def item(self, index: int) -> NavItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def currentRow(self) -> int:
        return self._current_row

    def setCurrentRow(self, index: int) -> None:
        if not 0 <= index < len(self._buttons):
            return
        changed = index != self._current_row
        self._current_row = index
        self._buttons[index].setChecked(True)
        if changed:
            self.currentRowChanged.emit(index)


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        paths: AppPaths,
        database: Database,
        file_store: FileStore,
        library_service: LibraryService,
        secret_store: SecretStore,
        worker: TaskWorker,
        client: NanoBananaClient,
        repository: MySQLCentralRepository,
        resolver: SharedPathResolver,
        actor: AuthUser,
        lazy_pages: bool = False,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.database = database
        self.file_store = file_store
        self.library_service = library_service
        self.secret_store = secret_store
        self.worker = worker
        self.client = client
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.lazy_pages = lazy_pages
        self._page_factories: dict[str, Callable[[], QWidget]] = {}
        self._page_attributes: dict[str, str] = {}
        self._page_indices: dict[str, int] = {}
        self._dirty_pages: set[str] = set()
        self._initial_data_loaded = False
        self._initial_load_scheduled = False
        self.setWindowIcon(QApplication.instance().windowIcon())
        self.setWindowTitle(APP_NAME)
        self.resize(1480, 920)
        self.setMinimumSize(1180, 720)

        self.stack = QStackedWidget()
        self.page_specs: list[tuple[str, QWidget]] = []
        role_allowed = set(
            ROLE_ALLOWED_MENUS.get(self.actor.role, ())
        )
        role_defaults = set(ROLE_DEFAULT_MENUS.get(self.actor.role, ()))
        configured = (
            set(role_defaults)
            if self.actor.menu_permissions is None
            else set(self.actor.menu_permissions)
        )
        allowed_menu_keys = configured & role_allowed
        label_keys = {
            "任务提交": "new_task",
            "提交记录": "submission_history",
            "待分配": "assign_tasks",
            "AI生图": "ai_generate",
            "普通创作": "create",
            "批量换装": "batch",
            "历史记录": "history",
            "作品库": "library",
            "管理控制台": "admin",
        }
        role_specs = self._available_page_specs()
        available_specs = [
            spec
            for spec in role_specs
            if label_keys.get(spec[0]) in allowed_menu_keys
        ]
        if not available_specs:
            empty_page = QWidget()
            empty_layout = QVBoxLayout(empty_page)
            empty_layout.addStretch(1)
            empty_message = QLabel(
                "当前账号还没有可查看的菜单，请联系管理员配置菜单权限。"
            )
            empty_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_message.setStyleSheet("color:#60708a;font-size:16px;")
            empty_layout.addWidget(empty_message)
            empty_layout.addStretch(1)
            available_specs = [
                ("无可用菜单", lambda: empty_page, "")
            ]
        specs_to_register = (
            role_specs
            if not self.lazy_pages
            else available_specs
        )
        for label, factory, attribute in specs_to_register:
            self._page_factories[label] = factory
            self._page_attributes[label] = attribute
        if not self.lazy_pages:
            available_labels = {
                label for label, _, _ in available_specs
            }
            for label, _factory, _attribute in role_specs:
                if label not in available_labels:
                    self._create_page(label)
        labels = [label for label, _, _ in available_specs]
        initial_label = labels[0]
        if self.actor.role == ROLE_AI and "AI生图" in labels:
            initial_label = "AI生图"
        elif self.actor.role == ROLE_ADMIN and "任务提交" in labels:
            initial_label = "任务提交"
        self._initial_page_label = initial_label
        for label, _factory, _attribute in available_specs:
            if self.lazy_pages and label != initial_label:
                page = QWidget()
                page.setProperty("lazy_page_placeholder", True)
            else:
                page = self._create_page(label)
            self._page_indices[label] = len(self.page_specs)
            self.page_specs.append((label, page))
            self.stack.addWidget(page)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 20, 24, 16)
        content_layout.addWidget(self.stack)
        root.addWidget(content, 1)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self._base_status_message = (
            f"{self.actor.display_name} · {ROLE_LABELS.get(self.actor.role, self.actor.role)} · "
            f"数据目录：{self.paths.root} · 生图请求不会自动重试"
        )
        self.statusBar().showMessage(self._base_status_message)

        self.nav_list.currentRowChanged.connect(self._nav_changed)
        self.worker.signals.task_changed.connect(self._on_task_changed)
        initial_index = self._page_indices[self._initial_page_label]
        self.nav_list.setCurrentRow(initial_index)

    def _available_page_specs(
        self,
    ) -> list[tuple[str, Callable[[], QWidget], str]]:
        specs: list[tuple[str, Callable[[], QWidget], str]] = [
            (
                "任务提交",
                lambda: TaskCreatePage(
                    self.repository,
                    self.resolver,
                    self.actor,
                    auto_load=False,
                    async_mode=self.lazy_pages,
                ),
                "new_task_page",
            )
        ]
        if self.actor.role in (ROLE_OPERATIONS, ROLE_ADMIN):
            specs.append(
                (
                    "提交记录",
                    lambda: SubmissionListPage(
                        self.repository,
                        self.resolver,
                        self.actor,
                        "records",
                        auto_load=False,
                        async_mode=self.lazy_pages,
                    ),
                    "submission_records_page",
                )
            )
        if self.actor.role in (ROLE_AI, ROLE_ADMIN):
            specs.extend(
                [
                    (
                        "待分配",
                        lambda: TaskAssignmentPage(
                            self.repository,
                            self.resolver,
                            self.actor,
                            auto_load=False,
                            async_mode=self.lazy_pages,
                        ),
                        "assignment_page",
                    ),
                    (
                        "AI生图",
                        lambda: AIGenerationPage(
                            self.repository,
                            self.resolver,
                            self.actor,
                            self.client,
                            self.secret_store,
                            self.database,
                            self.file_store,
                            self.library_service,
                            self.worker,
                            self.secret_store,
                            auto_load=False,
                            async_mode=self.lazy_pages,
                        ),
                        "ai_generation_page",
                    ),
                    (
                        "普通创作",
                        lambda: CreatePage(
                            self.database,
                            self.file_store,
                            self.library_service,
                            self.worker,
                            self.secret_store,
                            auto_load=not self.lazy_pages,
                            async_thumbnails=self.lazy_pages,
                        ),
                        "create_page",
                    ),
                    (
                        "批量换装",
                        lambda: BatchDressPage(
                            self.database,
                            self.file_store,
                            self.library_service,
                            self.worker,
                            self.secret_store,
                            auto_load=not self.lazy_pages,
                            async_thumbnails=self.lazy_pages,
                        ),
                        "batch_dress_page",
                    ),
                    (
                        "历史记录",
                        lambda: UnifiedHistoryPage(
                            self.database,
                            self.library_service,
                            self.worker,
                            self.repository,
                            self.resolver,
                            self.actor,
                            auto_load=not self.lazy_pages,
                            async_mode=self.lazy_pages,
                        ),
                        "history_page",
                    ),
                    (
                        "作品库",
                        lambda: LibraryPage(
                            self.database,
                            self.library_service,
                            auto_load=not self.lazy_pages,
                        ),
                        "library_page",
                    ),
                ]
            )
        if self.actor.role == ROLE_ADMIN:
            specs.append(
                (
                    "管理控制台",
                    lambda: AdminPage(
                        self.repository,
                        self.actor,
                        auto_load=False,
                    ),
                    "admin_page",
                )
            )
        return specs

    def _create_page(self, label: str) -> QWidget:
        page = self._page_factories[label]()
        attribute = self._page_attributes.get(label)
        if attribute:
            setattr(self, attribute, page)
        self._wire_page(label, page)
        return page

    def _wire_page(self, label: str, page: QWidget) -> None:
        if label == "待分配":
            page.projects_claimed.connect(self._on_projects_claimed)
        elif label == "AI生图":
            page.tasks_enqueued.connect(self._on_batch_enqueued)
        elif label == "普通创作":
            page.settings_requested.connect(self.open_settings)
            page.templates_changed.connect(self._on_templates_changed)
            page.library_changed.connect(self._on_library_changed)
        elif label == "批量换装":
            page.settings_requested.connect(self.open_settings)
            page.tasks_enqueued.connect(self._on_batch_enqueued)
            page.templates_changed.connect(self._on_templates_changed)
        elif label == "历史记录":
            page.library_changed.connect(self._on_library_changed)
            page.retry_requested.connect(self._open_retry)
            page.project_retry_requested.connect(self._open_project_retry)
            page.project_attempt_cancelled.connect(
                self._on_project_attempt_cancelled
            )

    def _on_projects_claimed(self, _count: int) -> None:
        self._mark_pages_dirty("AI生图", "提交记录", "历史记录")

    def _on_templates_changed(self) -> None:
        for label in ("普通创作", "批量换装", "AI生图"):
            page = self._existing_page(label)
            refresh = getattr(page, "refresh_templates", None)
            if callable(refresh):
                refresh()

    def _on_library_changed(self) -> None:
        self._mark_pages_dirty("作品库")

    def _on_project_attempt_cancelled(self, attempt_id: int) -> None:
        self._mark_pages_dirty("AI生图", "历史记录")
        page = self._existing_page("AI生图")
        remove = getattr(page, "remove_cancelled_attempt", None)
        if callable(remove):
            remove(attempt_id)

    def _existing_page(self, label: str) -> QWidget | None:
        index = self._page_indices.get(label)
        if index is None:
            return None
        page = self.page_specs[index][1]
        return (
            None
            if bool(page.property("lazy_page_placeholder"))
            else page
        )

    def _ensure_page(self, label: str) -> QWidget:
        page = self._existing_page(label)
        if page is not None:
            return page
        index = self._page_indices[label]
        placeholder = self.stack.widget(index)
        page = self._create_page(label)
        self.stack.removeWidget(placeholder)
        self.stack.insertWidget(index, page)
        placeholder.deleteLater()
        self.page_specs[index] = (label, page)
        self._dirty_pages.add(label)
        return page

    def _mark_pages_dirty(self, *labels: str) -> None:
        for label in labels:
            if label in self._page_indices:
                self._dirty_pages.add(label)

    def _build_sidebar(self) -> QWidget:
        labels = [text for text, _ in self.page_specs]
        self.nav_list = TopNavigation(
            labels,
            display_name=self.actor.display_name,
            role_label=ROLE_LABELS.get(
                self.actor.role,
                self.actor.role,
            ),
            settings_visible=self.actor.role in (ROLE_AI, ROLE_ADMIN),
        )
        self.nav_list.settings_requested.connect(self.open_settings)
        return self.nav_list

    def _nav_changed(self, index: int) -> None:
        if not 0 <= index < len(self.page_specs):
            return
        label = self.page_specs[index][0]
        created = False
        if self.lazy_pages:
            created = self._existing_page(label) is None
            page = self._ensure_page(label)
            self.stack.setCurrentWidget(page)
        else:
            self.stack.setCurrentIndex(index)
            page = self.page_specs[index][1]
        if not self._initial_data_loaded:
            return
        if not self.lazy_pages or created or label in self._dirty_pages:
            self._refresh_page(page)
            self._dirty_pages.discard(label)

    def _refresh_page(self, page: QWidget) -> None:
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            self.statusBar().showMessage("正在加载数据…")
            refresh()
            self.statusBar().showMessage(self._base_status_message)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._initial_load_scheduled:
            return
        self._initial_load_scheduled = True
        QTimer.singleShot(0, self._load_initial_page)

    def _load_initial_page(self) -> None:
        if self._initial_data_loaded:
            return
        self._initial_data_loaded = True
        label = self.page_specs[self.nav_list.currentRow()][0]
        page = (
            self._ensure_page(label)
            if self.lazy_pages
            else self.page_specs[self.nav_list.currentRow()][1]
        )
        self._refresh_page(page)
        self._dirty_pages.discard(label)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.database, self)
        dialog.exec()

    def _on_task_changed(self, task_id: int, status: str) -> None:
        label = STATUS_LABELS.get(status, status)
        self.statusBar().showMessage(
            f"任务 #{task_id}：{label} · 数据目录：{self.paths.root}", 8000
        )
        self._mark_pages_dirty(
            "提交记录",
            "AI生图",
            "历史记录",
            "作品库",
        )

    def _on_batch_enqueued(self, count: int) -> None:
        self.statusBar().showMessage(
            f"批量换装已加入 {count} 个排队任务 · 数据目录：{self.paths.root}",
            8000,
        )
        self._mark_pages_dirty(
            "提交记录",
            "AI生图",
            "历史记录",
            "作品库",
        )

    def _open_retry(self, task_id: int) -> None:
        task = self.database.get_task(task_id)
        if task and task["mode"] in (MODE_DRESS_UP, MODE_FREE_COMPOSE):
            if "批量换装" not in self._page_indices:
                return
            page = self._ensure_page("批量换装")
            self._select_page(page)
            page.prepare_retry(task_id)
            self.statusBar().showMessage(
                f"已载入批量换装任务 #{task_id}，修改后重新生成",
                8000,
            )
            return
        if "普通创作" not in self._page_indices:
            return
        page = self._ensure_page("普通创作")
        self._select_page(page)
        page.prepare_retry(task_id)
        self.statusBar().showMessage(
            f"已载入任务 #{task_id}，修改参数后点击生成",
            8000,
        )

    def _open_project_retry(
        self,
        task_id: int,
        attempt_id: int,
    ) -> None:
        if "AI生图" not in self._page_indices:
            QMessageBox.warning(
                self,
                "没有AI生图权限",
                "当前账号没有AI生图菜单权限，无法载入项目重试。",
            )
            return
        page = self._ensure_page("AI生图")
        try:
            page.prepare_history_retry(
                task_id,
                attempt_id,
            )
        except Exception as exc:
            QMessageBox.warning(self, "载入失败", str(exc))
            return
        self._select_page(page)
        self.statusBar().showMessage(
            "已载入项目子任务参数，请确认后手动加入生成队列",
            8000,
        )

    def _select_page(self, page: QWidget) -> None:
        for index, (_, candidate) in enumerate(self.page_specs):
            if candidate is page:
                self.nav_list.setCurrentRow(index)
                return

    def closeEvent(self, event) -> None:
        ai_page = self._existing_page("AI生图")
        project_running = bool(
            ai_page is not None
            and ai_page.is_generating()
        )
        if self.database.has_running_tasks() or project_running:
            answer = QMessageBox.warning(
                self,
                "仍有任务在执行",
                "关闭程序不会停止正在发送的生图请求，可能已经产生费用。\n"
                "确定强制退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.worker.shutdown()
        event.accept()
