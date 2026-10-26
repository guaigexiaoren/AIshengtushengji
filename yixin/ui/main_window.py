from __future__ import annotations

from PySide6.QtCore import Qt, Signal
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
    ROLE_LABELS,
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
from .history_page import HistoryPage
from .library_page import LibraryPage
from .submission_list_page import SubmissionListPage
from .task_center_page import TaskCenterPage
from .task_create_page import TaskCreatePage


class NavItem:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class SidebarNav(QWidget):
    currentRowChanged = Signal(int)

    def __init__(
        self,
        labels: list[str],
        parent: QWidget | None = None,
        *,
        group_before: dict[int, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._current_row = -1
        self._buttons: list[QPushButton] = []
        self._items = [NavItem(text) for text in labels]
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        groups = group_before or {}
        for index, text in enumerate(labels):
            if index in groups:
                group_label = QLabel(groups[index])
                group_label.setObjectName("NavGroup")
                layout.addWidget(group_label)
            button = QPushButton(text)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setFixedHeight(38)
            button.clicked.connect(
                lambda _checked=False, row=index: self.setCurrentRow(row)
            )
            self._button_group.addButton(button, index)
            self._buttons.append(button)
            layout.addWidget(button)
        layout.addStretch(1)

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
    ) -> None:
        super().__init__()
        self.paths = paths
        self.database = database
        self.secret_store = secret_store
        self.worker = worker
        self.client = client
        self.repository = repository
        self.resolver = resolver
        self.actor = actor
        self.setWindowIcon(QApplication.instance().windowIcon())
        self.setWindowTitle(APP_NAME)
        self.resize(1480, 920)
        self.setMinimumSize(1180, 720)

        self.stack = QStackedWidget()
        self.page_specs: list[tuple[str, QWidget]] = []
        self.new_task_page = TaskCreatePage(
            repository,
            resolver,
            actor,
        )
        self.page_specs.append(("新建任务", self.new_task_page))
        self.submitted_page = SubmissionListPage(
            repository,
            resolver,
            actor,
            "submitted",
        )
        self.pending_page = SubmissionListPage(
            repository,
            resolver,
            actor,
            "pending",
        )
        self.completed_page = SubmissionListPage(
            repository,
            resolver,
            actor,
            "completed",
        )
        self.page_specs.extend(
            [
                ("已提交", self.submitted_page),
                ("待审核", self.pending_page),
                ("已完成", self.completed_page),
            ]
        )
        self.task_center_page = TaskCenterPage(
            repository,
            resolver,
            actor,
            client,
            secret_store,
            file_store,
        )
        self.page_specs.append(("任务中心", self.task_center_page))
        if self.actor.role in (ROLE_AI, ROLE_ADMIN):
            self.create_page = CreatePage(
                database,
                file_store,
                library_service,
                worker,
                secret_store,
            )
            self.batch_dress_page = BatchDressPage(
                database,
                file_store,
                library_service,
                worker,
                secret_store,
            )
            self.history_page = HistoryPage(database, library_service, worker)
            self.library_page = LibraryPage(database, library_service)
            self.page_specs.extend(
                [
                    ("普通创作", self.create_page),
                    ("批量换装", self.batch_dress_page),
                    ("历史记录", self.history_page),
                    ("作品库", self.library_page),
                ]
            )
        if self.actor.role == ROLE_ADMIN:
            self.admin_page = AdminPage(repository, actor)
            self.page_specs.append(("管理控制台", self.admin_page))
        for _, page in self.page_specs:
            self.stack.addWidget(page)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        root.addWidget(self.stack, 1)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 18, 18, 12)
        content_layout.addWidget(self.stack)
        root.removeWidget(self.stack)
        root.addWidget(content, 1)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(
            f"{self.actor.display_name} · {ROLE_LABELS.get(self.actor.role, self.actor.role)} · "
            f"数据目录：{self.paths.root} · 生图请求不会自动重试"
        )

        self.nav_list.currentRowChanged.connect(self._nav_changed)
        if hasattr(self, "create_page"):
            self.create_page.settings_requested.connect(self.open_settings)
            self.batch_dress_page.settings_requested.connect(self.open_settings)
            self.batch_dress_page.tasks_enqueued.connect(self._on_batch_enqueued)
            self.create_page.templates_changed.connect(
                self.batch_dress_page.refresh_templates
            )
            self.batch_dress_page.templates_changed.connect(
                self.create_page.refresh_templates
            )
            self.create_page.library_changed.connect(self.library_page.refresh)
            self.history_page.library_changed.connect(self.library_page.refresh)
            self.history_page.retry_requested.connect(self._open_retry)
        self.worker.signals.task_changed.connect(self._on_task_changed)
        if self.actor.role in (ROLE_AI, ROLE_ADMIN):
            initial_page = (
                self.task_center_page
                if self.actor.role == ROLE_AI
                else self.new_task_page
            )
        else:
            initial_page = self.new_task_page
        self._select_page(initial_page)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 24, 14, 18)
        layout.setSpacing(12)
        title = QLabel("亦欣 AI")
        title.setObjectName("BrandTitle")
        subtitle = QLabel(
            f"{self.actor.display_name} · {ROLE_LABELS.get(self.actor.role, self.actor.role)}"
        )
        subtitle.setObjectName("BrandSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(20)
        labels = [text for text, _ in self.page_specs]
        group_index = 0
        self.nav_list = SidebarNav(
            labels,
            group_before={group_index: "AI图提交"},
        )
        self.nav_list.setObjectName("NavList")
        layout.addWidget(self.nav_list)
        layout.addStretch(1)
        note = QLabel("任务候选图保留 30 天")
        note.setStyleSheet("color:#91a3bf;font-size:11px;")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(note)
        if self.actor.role in (ROLE_AI, ROLE_ADMIN):
            settings = QPushButton("设置")
            settings.setObjectName("GhostButton")
            settings.clicked.connect(self.open_settings)
            layout.addWidget(settings)
        return sidebar

    def _nav_changed(self, index: int) -> None:
        if not 0 <= index < len(self.page_specs):
            return
        self.stack.setCurrentIndex(index)
        page = self.page_specs[index][1]
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.database, self)
        dialog.exec()

    def _on_task_changed(self, task_id: int, status: str) -> None:
        label = STATUS_LABELS.get(status, status)
        self.statusBar().showMessage(
            f"任务 #{task_id}：{label} · 数据目录：{self.paths.root}", 8000
        )

    def _on_batch_enqueued(self, count: int) -> None:
        self.statusBar().showMessage(
            f"批量换装已加入 {count} 个排队任务 · 数据目录：{self.paths.root}",
            8000,
        )

    def _open_retry(self, task_id: int) -> None:
        if not hasattr(self, "create_page"):
            return
        task = self.database.get_task(task_id)
        if task and task["mode"] in (MODE_DRESS_UP, MODE_FREE_COMPOSE):
            self._select_page(self.batch_dress_page)
            self.batch_dress_page.prepare_retry(task_id)
            self.statusBar().showMessage(
                f"已载入批量换装任务 #{task_id}，修改后重新生成",
                8000,
            )
            return
        self._select_page(self.create_page)
        self.create_page.prepare_retry(task_id)
        self.statusBar().showMessage(
            f"已载入任务 #{task_id}，修改参数后点击生成",
            8000,
        )

    def _select_page(self, page: QWidget) -> None:
        for index, (_, candidate) in enumerate(self.page_specs):
            if candidate is page:
                self.nav_list.setCurrentRow(index)
                return

    def closeEvent(self, event) -> None:
        project_running = bool(
            hasattr(self, "task_center_page")
            and self.task_center_page.is_generating()
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
