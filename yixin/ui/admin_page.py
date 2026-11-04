from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.types import (
    PROJECT_STATUS_LABELS,
    ROLE_ADMIN,
    ROLE_AI,
    ROLE_LABELS,
    ROLE_OPERATIONS,
    TASK_STATUS_LABELS,
    ApiProvider,
    AuthUser,
    MENU_KEYS,
    PROJECT_SCOPE_LABELS,
    PROJECT_SCOPES,
    ROLE_ALLOWED_MENUS,
    ROLE_DEFAULT_MENUS,
    ProjectRecord,
    ProjectTask,
)
from ..constants import (
    MODEL_OPTIONS,
    PROVIDER_PROTOCOL_LABELS,
    PROVIDER_PROTOCOLS,
)
from ..security import validate_api_key
from ..time_utils import utc_naive_to_local
from .async_queries import get_query_runner
from .components import PageActionBar, PageHeader


class UserPermissionOptions(QWidget):
    def __init__(
        self,
        role_combo: QComboBox,
        menu_permissions: tuple[str, ...],
        project_access: tuple[tuple[str, str], ...],
        shared_projects: dict[str, tuple[str, ...]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.role_combo = role_combo
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        menu_group = QGroupBox("菜单查看权限")
        menu_layout = QGridLayout(menu_group)
        self.menu_checks: dict[str, QCheckBox] = {}
        selected_menus = set(menu_permissions)
        for index, (key, label) in enumerate(MENU_KEYS.items()):
            check = QCheckBox(label)
            check.setChecked(key in selected_menus)
            self.menu_checks[key] = check
            menu_layout.addWidget(check, index // 3, index % 3)
        layout.addWidget(menu_group)

        self.project_group = QGroupBox("共享盘项目权限")
        project_layout = QVBoxLayout(self.project_group)
        project_hint = QLabel(
            "项目来自共享盘“内部项目/外部项目”下的一级文件夹。"
        )
        project_hint.setStyleSheet("color:#60708a;")
        project_layout.addWidget(project_hint)
        project_scroll = QScrollArea()
        project_scroll.setWidgetResizable(True)
        project_content = QWidget()
        project_checks_layout = QVBoxLayout(project_content)
        selected_projects = set(project_access)
        self.project_checks: dict[tuple[str, str], QCheckBox] = {}
        available = {
            (scope, name)
            for scope in PROJECT_SCOPES
            for name in shared_projects.get(scope, ())
        }
        available.update(selected_projects)
        for scope in PROJECT_SCOPES:
            names = sorted(
                (
                    name
                    for item_scope, name in available
                    if item_scope == scope
                ),
                key=str.casefold,
            )
            heading = QLabel(PROJECT_SCOPE_LABELS[scope])
            heading.setStyleSheet("font-weight:700;padding-top:6px;")
            project_checks_layout.addWidget(heading)
            if not names:
                project_checks_layout.addWidget(
                    QLabel("共享盘中没有发现项目文件夹")
                )
            for name in names:
                key = (scope, name)
                check = QCheckBox(name)
                check.setChecked(key in selected_projects)
                self.project_checks[key] = check
                project_checks_layout.addWidget(check)
        project_checks_layout.addStretch(1)
        project_scroll.setWidget(project_content)
        project_layout.addWidget(project_scroll, 1)
        layout.addWidget(self.project_group, 1)

        self._last_role = str(role_combo.currentData())
        self._apply_role_state(self._last_role)
        self.role_combo.currentIndexChanged.connect(self._role_changed)

    def _role_changed(self) -> None:
        role = str(self.role_combo.currentData())
        if role == self._last_role:
            return
        self._last_role = role
        defaults = set(ROLE_DEFAULT_MENUS.get(role, ()))
        for key, check in self.menu_checks.items():
            check.setChecked(key in defaults)
        self._apply_role_state(role)

    def _apply_role_state(self, role: str) -> None:
        allowed = set(ROLE_ALLOWED_MENUS.get(role, ()))
        for key, check in self.menu_checks.items():
            check.setEnabled(key in allowed)
            if key not in allowed:
                check.setChecked(False)
        self.project_group.setEnabled(role == ROLE_OPERATIONS)

    def selected_menu_permissions(self) -> tuple[str, ...]:
        return tuple(
            key
            for key, check in self.menu_checks.items()
            if check.isEnabled() and check.isChecked()
        )

    def selected_project_access(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            key
            for key, check in self.project_checks.items()
            if check.isChecked()
        )


class AddUserDialog(QDialog):
    def __init__(
        self,
        shared_projects: dict[str, tuple[str, ...]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("新增账号")
        self.resize(680, 780)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.username = QLineEdit()
        self.display_name = QLineEdit()
        self.role = QComboBox()
        for role in (ROLE_OPERATIONS, ROLE_AI, ROLE_ADMIN):
            self.role.addItem(ROLE_LABELS[role], role)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("至少 6 个字符，首次登录后强制修改")
        self.urgent_quota = QSpinBox()
        self.urgent_quota.setRange(0, 99)
        self.urgent_quota.setValue(3)
        self.max_concurrency = QSpinBox()
        self.max_concurrency.setRange(1, 10)
        self.max_concurrency.setValue(2)
        form.addRow("登录账号", self.username)
        form.addRow("显示名称", self.display_name)
        form.addRow("角色", self.role)
        form.addRow("临时密码", self.password)
        form.addRow("每日加急额度", self.urgent_quota)
        form.addRow("最大生成并发", self.max_concurrency)
        layout.addLayout(form)
        self.permissions = UserPermissionOptions(
            self.role,
            ROLE_DEFAULT_MENUS.get(
                self.role.currentData(),
                (),
            ),
            (),
            shared_projects or {},
        )
        layout.addWidget(self.permissions, 1)
        actions = PageActionBar()
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        save.setObjectName("PrimaryButton")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        actions.layout.addStretch(1)
        actions.layout.addWidget(cancel)
        actions.layout.addWidget(save)
        layout.addWidget(actions)

    def selected_menu_permissions(self) -> tuple[str, ...]:
        return self.permissions.selected_menu_permissions()

    def selected_project_access(self) -> tuple[tuple[str, str], ...]:
        return self.permissions.selected_project_access()


class EditUserDialog(QDialog):
    def __init__(
        self,
        user: AuthUser,
        menu_permissions: tuple[str, ...],
        project_access: tuple[tuple[str, str], ...],
        shared_projects: dict[str, tuple[str, ...]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.user = user
        self.setWindowTitle(f"编辑账号 · {user.display_name}")
        self.resize(680, 780)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.username = QLineEdit(user.username)
        self.display_name = QLineEdit(user.display_name)
        self.role = QComboBox()
        for role in (ROLE_OPERATIONS, ROLE_AI, ROLE_ADMIN):
            self.role.addItem(ROLE_LABELS[role], role)
        self.role.setCurrentIndex(self.role.findData(user.role))
        self.active = QCheckBox("启用账号")
        self.active.setChecked(user.is_active)
        self.urgent_quota = QSpinBox()
        self.urgent_quota.setRange(0, 99)
        self.urgent_quota.setValue(user.urgent_quota_per_day)
        self.max_concurrency = QSpinBox()
        self.max_concurrency.setRange(1, 10)
        self.max_concurrency.setValue(user.max_generation_concurrency)
        form.addRow("登录账号", self.username)
        form.addRow("显示名称", self.display_name)
        form.addRow("角色", self.role)
        form.addRow("账号状态", self.active)
        form.addRow("每日加急额度", self.urgent_quota)
        form.addRow("最大生成并发", self.max_concurrency)
        layout.addLayout(form)

        self.permissions = UserPermissionOptions(
            self.role,
            menu_permissions,
            project_access,
            shared_projects,
        )
        layout.addWidget(self.permissions, 1)

        actions = PageActionBar()
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        save.setObjectName("PrimaryButton")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        actions.layout.addStretch(1)
        actions.layout.addWidget(cancel)
        actions.layout.addWidget(save)
        layout.addWidget(actions)

    def selected_menu_permissions(self) -> tuple[str, ...]:
        return self.permissions.selected_menu_permissions()

    def selected_project_access(self) -> tuple[tuple[str, str], ...]:
        return self.permissions.selected_project_access()


class ApiKeyDialog(QDialog):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        actor: AuthUser,
        target: AuthUser,
        providers: tuple[ApiProvider, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.actor = actor
        self.target = target
        self.setWindowTitle(f"配置 API Key · {target.display_name}")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        tip = QLabel(
            "密钥将以加密形式保存到数据库，界面只显示尾号。"
            "任何人员都不能通过程序界面查看完整密钥。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)
        form = QFormLayout()
        self.provider_combo = QComboBox()
        for provider in providers:
            self.provider_combo.addItem(
                f"{provider.display_name} · {provider.base_url}",
                provider.id,
            )
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("粘贴新的 API Key")
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color:#60708a;")
        form.addRow("API 平台", self.provider_combo)
        form.addRow("当前状态", self.status_label)
        form.addRow("API Key", self.key_edit)
        layout.addLayout(form)
        actions = PageActionBar()
        check = QPushButton("检查格式")
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        save.setObjectName("PrimaryButton")
        cancel.clicked.connect(self.reject)
        check.clicked.connect(self._check_format)
        save.clicked.connect(self._save)
        self.provider_combo.currentIndexChanged.connect(
            self._refresh_status
        )
        actions.layout.addStretch(1)
        actions.layout.addWidget(check)
        actions.layout.addWidget(cancel)
        actions.layout.addWidget(save)
        layout.addWidget(actions)
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.key_edit.clear()
        provider_id = self.provider_combo.currentData()
        if provider_id is None:
            self.status_label.setText("没有已启用的 API 平台")
            self.key_edit.setEnabled(False)
            return
        self.key_edit.setEnabled(True)
        try:
            status = self.repository.api_key_status(
                self.target.id,
                int(provider_id),
            )
        except Exception:
            status = None
        if not status:
            self.status_label.setText("未配置")
            return
        self.status_label.setText(
            f"已配置 · 尾号 {status['key_suffix']} · "
            f"{utc_naive_to_local(status['updated_at']).strftime('%Y-%m-%d %H:%M')}"
        )

    def _check_format(self) -> None:
        try:
            validate_api_key(self.key_edit.text())
        except Exception as exc:
            QMessageBox.warning(self, "格式不正确", str(exc))
            return
        QMessageBox.information(self, "格式正确", "API Key 格式检查通过。")

    def _save(self) -> None:
        provider_id = self.provider_combo.currentData()
        if provider_id is None:
            QMessageBox.warning(
                self,
                "没有可用平台",
                "请先启用至少一个 API 平台。",
            )
            return
        try:
            self.repository.save_api_key(
                self.actor,
                self.target.id,
                self.key_edit.text(),
                provider_id=int(provider_id),
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()


class ApiProviderDialog(QDialog):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        actor: AuthUser,
        provider: ApiProvider,
        models: list[dict],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.actor = actor
        self.provider = provider
        self.setWindowTitle(f"编辑 API 平台 · {provider.display_name}")
        self.resize(920, 560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.display_name = QLineEdit(provider.display_name)
        self.base_url = QLineEdit(provider.base_url)
        self.default_protocol = QComboBox()
        for protocol in PROVIDER_PROTOCOLS:
            self.default_protocol.addItem(
                PROVIDER_PROTOCOL_LABELS[protocol],
                protocol,
            )
        self.default_protocol.setCurrentIndex(
            self.default_protocol.findData(provider.default_protocol)
        )
        self.priority = QSpinBox()
        self.priority.setRange(0, 9999)
        self.priority.setValue(provider.priority)
        self.enabled = QCheckBox("启用该平台")
        self.enabled.setChecked(provider.enabled)
        form.addRow("平台名称", self.display_name)
        form.addRow("API 地址", self.base_url)
        form.addRow("默认协议", self.default_protocol)
        form.addRow("平台优先级", self.priority)
        form.addRow("平台状态", self.enabled)
        layout.addLayout(form)

        hint = QLabel(
            "“逻辑模型”是页面显示的模型；“平台模型名”是该平台实际"
            "接收的模型名称。协议、参考图上限必须按平台文档填写。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#60708a;")
        layout.addWidget(hint)
        self.model_table = QTableWidget(0, 6)
        self.model_table.setHorizontalHeaderLabels(
            [
                "逻辑模型",
                "平台模型名",
                "请求协议",
                "最大参考图",
                "单次出图",
                "启用",
            ]
        )
        self.model_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((190, 260, 180, 100, 85, 60)):
            self.model_table.setColumnWidth(column, width)
        layout.addWidget(self.model_table, 1)

        existing = {
            str(model["logical_model"]): model for model in models
        }
        for label, logical_model in MODEL_OPTIONS:
            model = existing.get(logical_model, {})
            row = self.model_table.rowCount()
            self.model_table.insertRow(row)
            name_item = QTableWidgetItem(label)
            name_item.setData(Qt.ItemDataRole.UserRole, logical_model)
            name_item.setFlags(
                name_item.flags()
                & ~Qt.ItemFlag.ItemIsEditable
            )
            self.model_table.setItem(row, 0, name_item)
            provider_model = QLineEdit(
                str(model.get("provider_model") or logical_model)
            )
            self.model_table.setCellWidget(row, 1, provider_model)
            protocol = QComboBox()
            for value in PROVIDER_PROTOCOLS:
                protocol.addItem(
                    PROVIDER_PROTOCOL_LABELS[value],
                    value,
                )
            protocol.setCurrentIndex(
                max(
                    0,
                    protocol.findData(
                        model.get("protocol")
                        or provider.default_protocol
                    ),
                )
            )
            self.model_table.setCellWidget(row, 2, protocol)
            max_references = QSpinBox()
            max_references.setRange(0, 14)
            max_references.setValue(
                14
                if model.get("max_reference_images") is None
                else int(model["max_reference_images"])
            )
            self.model_table.setCellWidget(row, 3, max_references)
            images_per_request = QSpinBox()
            images_per_request.setRange(1, 4)
            images_per_request.setValue(
                1
                if model.get("images_per_request") is None
                else int(model["images_per_request"])
            )
            self.model_table.setCellWidget(row, 4, images_per_request)
            model_enabled = QCheckBox()
            model_enabled.setChecked(bool(model.get("is_enabled", True)))
            self.model_table.setCellWidget(row, 5, model_enabled)

        actions = QHBoxLayout()
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        save.setObjectName("PrimaryButton")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def _save(self) -> None:
        models: list[dict] = []
        for row in range(self.model_table.rowCount()):
            logical_item = self.model_table.item(row, 0)
            provider_model = self.model_table.cellWidget(row, 1)
            protocol = self.model_table.cellWidget(row, 2)
            max_references = self.model_table.cellWidget(row, 3)
            images_per_request = self.model_table.cellWidget(row, 4)
            model_enabled = self.model_table.cellWidget(row, 5)
            models.append(
                {
                    "logical_model": logical_item.data(
                        Qt.ItemDataRole.UserRole
                    ),
                    "provider_model": provider_model.text(),
                    "protocol": protocol.currentData(),
                    "max_reference_images": max_references.value(),
                    "images_per_request": images_per_request.value(),
                    "priority": self.priority.value(),
                    "enabled": model_enabled.isChecked(),
                }
            )
        try:
            self.repository.update_api_provider(
                self.actor,
                self.provider.id,
                display_name=self.display_name.text(),
                base_url=self.base_url.text(),
                default_protocol=self.default_protocol.currentData(),
                enabled=self.enabled.isChecked(),
                priority=self.priority.value(),
                models=models,
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()


class AdminPage(QWidget):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        actor: AuthUser,
        parent: QWidget | None = None,
        *,
        auto_load: bool = True,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.actor = actor
        self.projects: list[ProjectRecord] = []
        self.tasks: list[ProjectTask] = []
        self.users: list[AuthUser] = []
        self.api_key_statuses: dict[int, dict] = {}
        self.api_key_statuses_by_provider: dict[
            tuple[int, int], dict
        ] = {}
        self.api_providers: list[ApiProvider] = []
        self.query_runner = get_query_runner()
        self._active_exception_request = -1
        self._build()
        self.query_runner.succeeded.connect(self._query_succeeded)
        self.query_runner.failed.connect(self._query_failed)
        if auto_load:
            self.refresh_all()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(
            PageHeader(
                "管理控制台",
                "账号、API 平台、共享设置和任务异常。",
            )
        )
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_users_tab(), "用户与权限")
        self.tabs.addTab(self._build_api_platforms_tab(), "API 平台")
        self.tabs.addTab(self._build_settings_tab(), "价格与共享设置")
        self.tabs.addTab(self._build_exceptions_tab(), "任务异常")
        layout.addWidget(self.tabs, 1)

    def refresh_all(self) -> None:
        self._refresh_users()
        self._refresh_api_platforms()
        self._refresh_prices()
        self._refresh_settings()
        self._refresh_templates()
        self._refresh_projects()
    def refresh(self) -> None:
        self.refresh_all()

    def _build_users_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        toolbar = QHBoxLayout()
        add_button = QPushButton("新增账号")
        add_button.clicked.connect(self._add_user)
        edit_button = QPushButton("编辑账号")
        edit_button.clicked.connect(self._edit_user)
        self.toggle_user_button = QPushButton("停用/启用")
        self.toggle_user_button.clicked.connect(self._toggle_user)
        reset_button = QPushButton("重置密码")
        reset_button.clicked.connect(self._reset_password)
        self.api_key_button = QPushButton("配置 API Key")
        self.api_key_button.clicked.connect(self._configure_api_key)
        self.clear_api_key_button = QPushButton("清除 API Key")
        self.clear_api_key_button.clicked.connect(self._clear_api_key)
        self.delete_user_button = QPushButton("删除账号")
        self.delete_user_button.setObjectName("DangerButton")
        self.delete_user_button.clicked.connect(self._delete_user)
        self.sync_operations_button = QPushButton(
            "扫描共享盘并创建运营账号"
        )
        self.sync_operations_button.clicked.connect(
            self._sync_operation_accounts
        )
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self._refresh_users)
        toolbar.addStretch(1)
        toolbar.addWidget(refresh_button)
        layout.addLayout(toolbar)
        self.user_table = QTableWidget(0, 9)
        self.user_table.setHorizontalHeaderLabels(
            [
                "账号",
                "姓名",
                "角色",
                "状态",
                "首次改密",
                "API Key",
                "密钥更新时间",
                "每日加急",
                "并发上限",
            ]
        )
        self.user_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.user_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.user_table.horizontalHeader().setStretchLastSection(True)
        self.user_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate(
            (120, 130, 80, 70, 85, 120, 145, 80, 80)
        ):
            self.user_table.setColumnWidth(column, width)
        layout.addWidget(self.user_table, 1)
        action_bar = PageActionBar()
        for button in (
            add_button,
            self.sync_operations_button,
            edit_button,
            self.toggle_user_button,
            reset_button,
            self.api_key_button,
            self.clear_api_key_button,
            self.delete_user_button,
        ):
            action_bar.layout.addWidget(button)
        action_bar.layout.addStretch(1)
        layout.addWidget(action_bar)
        return widget

    def _build_api_platforms_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        hint = QLabel(
            "平台优先级数字越小越先调用。生成发生网络、额度或服务端错误时，"
            "会按顺序切换到下一个已配置 Key 的平台；请求超时不会自动切换。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#60708a;")
        layout.addWidget(hint)
        toolbar = QHBoxLayout()
        edit = QPushButton("编辑所选平台")
        refresh = QPushButton("刷新")
        edit.clicked.connect(self._edit_api_provider)
        refresh.clicked.connect(self._refresh_api_platforms)
        toolbar.addStretch(1)
        toolbar.addWidget(refresh)
        layout.addLayout(toolbar)
        self.provider_table = QTableWidget(0, 6)
        self.provider_table.setHorizontalHeaderLabels(
            ["平台", "协议", "地址", "状态", "优先级", "模型映射"]
        )
        self.provider_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.provider_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.provider_table.horizontalHeader().setStretchLastSection(True)
        self.provider_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((120, 180, 340, 80, 80, 100)):
            self.provider_table.setColumnWidth(column, width)
        layout.addWidget(self.provider_table, 1)
        action_bar = PageActionBar()
        action_bar.layout.addWidget(edit)
        action_bar.layout.addStretch(1)
        layout.addWidget(action_bar)
        return widget

    def _build_settings_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        share_card = QGroupBox("共享盘与数据库")
        share_layout = QFormLayout(share_card)
        self.share_root_edit = QLineEdit()
        self.share_root_edit.setPlaceholderText(
            "例如 \\\\fileserver\\AI项目"
        )
        share_layout.addRow("统一 UNC 根目录", self.share_root_edit)
        self.internal_project_label = QLabel()
        self.external_project_label = QLabel()
        self.internal_project_label.setStyleSheet("color:#60708a;")
        self.external_project_label.setStyleSheet("color:#60708a;")
        share_layout.addRow("内部项目目录", self.internal_project_label)
        share_layout.addRow("外部项目目录", self.external_project_label)
        share_actions = QHBoxLayout()
        save_share = QPushButton("保存共享目录")
        save_share.clicked.connect(self._save_share_root)
        scan_projects = QPushButton("扫描项目文件夹")
        scan_projects.clicked.connect(self._scan_shared_projects)
        test_connection = QPushButton("测试数据库连接")
        test_connection.clicked.connect(self._test_database)
        share_actions.addWidget(save_share)
        share_actions.addWidget(scan_projects)
        share_actions.addWidget(test_connection)
        share_actions.addStretch(1)
        share_layout.addRow("", share_actions)
        layout.addWidget(share_card)

        price_card = QGroupBox("模型单图成本（人民币）")
        price_layout = QVBoxLayout(price_card)
        self.price_inputs: dict[str, QDoubleSpinBox] = {}
        for label, model_id in MODEL_OPTIONS:
            row = QHBoxLayout()
            row.addWidget(QLabel(label), 1)
            spin = QDoubleSpinBox()
            spin.setRange(0, 100000)
            spin.setDecimals(4)
            spin.setSingleStep(0.1)
            self.price_inputs[model_id] = spin
            row.addWidget(spin)
            price_layout.addLayout(row)
        save_prices = QPushButton("保存当前价格")
        save_prices.clicked.connect(self._save_prices)
        price_layout.addWidget(save_prices)
        layout.addWidget(price_card)

        template_card = QGroupBox("共享提示词模板")
        template_layout = QVBoxLayout(template_card)
        self.template_list = QListWidget()
        template_layout.addWidget(self.template_list, 1)
        template_actions = QHBoxLayout()
        add_template = QPushButton("新增")
        edit_template = QPushButton("编辑")
        delete_template = QPushButton("删除")
        add_template.clicked.connect(self._add_template)
        edit_template.clicked.connect(self._edit_template)
        delete_template.clicked.connect(self._delete_template)
        template_actions.addWidget(add_template)
        template_actions.addWidget(edit_template)
        template_actions.addWidget(delete_template)
        template_actions.addStretch(1)
        template_layout.addLayout(template_actions)
        layout.addWidget(template_card, 1)
        return widget

    def _build_exceptions_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.project_table = QTableWidget(0, 7)
        self.project_table.setHorizontalHeaderLabels(
            ["任务编码", "货号", "运营", "状态", "子任务进度", "加急", "日期"]
        )
        self.project_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.project_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.project_table.itemSelectionChanged.connect(
            self._admin_project_selected
        )
        self.project_table.horizontalHeader().setStretchLastSection(True)
        self.project_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((230, 90, 120, 100, 110, 70, 100)):
            self.project_table.setColumnWidth(column, width)
        splitter.addWidget(self.project_table)
        self.admin_task_table = QTableWidget(0, 6)
        self.admin_task_table.setHorizontalHeaderLabels(
            ["任务", "服装", "模特", "制作人员", "状态", "最终数量"]
        )
        self.admin_task_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.admin_task_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.admin_task_table.horizontalHeader().setStretchLastSection(True)
        self.admin_task_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((240, 150, 150, 120, 100, 80)):
            self.admin_task_table.setColumnWidth(column, width)
        splitter.addWidget(self.admin_task_table)
        layout.addWidget(splitter, 1)
        actions = PageActionBar()
        reassign = QPushButton("改派所选任务")
        cancel = QPushButton("取消所选任务")
        reopen = QPushButton("重开所选任务")
        refresh = QPushButton("刷新")
        reassign.clicked.connect(self._reassign_task)
        cancel.clicked.connect(self._admin_cancel_project)
        reopen.clicked.connect(self._reopen_project)
        refresh.clicked.connect(self._refresh_projects)
        actions.layout.addWidget(reassign)
        actions.layout.addStretch(1)
        actions.layout.addWidget(refresh)
        actions.layout.addWidget(cancel)
        actions.layout.addWidget(reopen)
        layout.addWidget(actions)
        return widget

    def _refresh_users(self) -> None:
        try:
            self.users = self.repository.list_users()
            self.api_key_statuses = self.repository.api_key_statuses()
            self.api_key_statuses_by_provider = (
                self.repository.api_key_statuses_by_provider()
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取账号失败", str(exc))
            return
        self.user_table.setRowCount(0)
        for user in self.users:
            row = self.user_table.rowCount()
            self.user_table.insertRow(row)
            key_info = self.api_key_statuses.get(user.id)
            provider_count = sum(
                1
                for user_id, _provider_id
                in self.api_key_statuses_by_provider
                if user_id == user.id
            )
            values = (
                user.username,
                user.display_name,
                ROLE_LABELS.get(user.role, user.role),
                "启用" if user.is_active else "停用",
                "是" if user.must_change_password else "否",
                (
                    f"已配置 {provider_count} 个平台"
                    f" · {key_info['key_suffix']}"
                    if key_info
                    else "未配置"
                ),
                (
                    utc_naive_to_local(key_info["updated_at"]).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if key_info
                    else ""
                ),
                str(user.urgent_quota_per_day),
                str(user.max_generation_concurrency),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, user.id)
                self.user_table.setItem(row, column, item)

    def _refresh_api_platforms(self) -> None:
        try:
            self.api_providers = self.repository.list_api_providers()
            model_counts = {
                provider.id: len(
                    self.repository.list_provider_models(
                        provider.id,
                        include_disabled=False,
                    )
                )
                for provider in self.api_providers
            }
        except Exception as exc:
            QMessageBox.warning(self, "读取 API 平台失败", str(exc))
            return
        self.provider_table.setRowCount(0)
        for provider in self.api_providers:
            row = self.provider_table.rowCount()
            self.provider_table.insertRow(row)
            values = (
                provider.display_name,
                PROVIDER_PROTOCOL_LABELS.get(
                    provider.default_protocol,
                    provider.default_protocol,
                ),
                provider.base_url,
                "启用" if provider.enabled else "停用",
                str(provider.priority),
                f"{model_counts.get(provider.id, 0)} 个",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        provider.id,
                    )
                self.provider_table.setItem(row, column, item)

    def _current_provider(self) -> ApiProvider | None:
        row = self.provider_table.currentRow()
        if not 0 <= row < len(self.api_providers):
            return None
        return self.api_providers[row]

    def _edit_api_provider(self) -> None:
        provider = self._current_provider()
        if provider is None:
            QMessageBox.information(
                self,
                "未选择平台",
                "请先选择一个 API 平台。",
            )
            return
        try:
            models = self.repository.list_provider_models(provider.id)
        except Exception as exc:
            QMessageBox.warning(self, "读取模型映射失败", str(exc))
            return
        dialog = ApiProviderDialog(
            self.repository,
            self.actor,
            provider,
            models,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_api_platforms()

    def _current_user(self) -> AuthUser | None:
        row = self.user_table.currentRow()
        if not 0 <= row < len(self.users):
            return None
        return self.users[row]

    def _add_user(self) -> None:
        try:
            shared_projects = self.repository.list_shared_projects()
        except Exception as exc:
            QMessageBox.warning(self, "读取共享盘项目失败", str(exc))
            return
        dialog = AddUserDialog(shared_projects, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.repository.create_user(
                self.actor,
                dialog.username.text(),
                dialog.display_name.text(),
                dialog.role.currentData(),
                dialog.password.text(),
                urgent_quota_per_day=dialog.urgent_quota.value(),
                max_generation_concurrency=dialog.max_concurrency.value(),
                menu_permissions=dialog.selected_menu_permissions(),
                project_access=dialog.selected_project_access(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "新增失败", str(exc))
            return
        self._refresh_users()

    def _sync_operation_accounts(self) -> None:
        try:
            candidates = self.repository.preview_operation_accounts(
                self.actor
            )
        except Exception as exc:
            QMessageBox.warning(self, "扫描共享盘失败", str(exc))
            return
        if not candidates:
            QMessageBox.information(
                self,
                "没有项目文件夹",
                "共享盘中没有扫描到内部或外部项目文件夹。",
            )
            return
        conflicts = [
            item for item in candidates if item.status == "conflict"
        ]
        if conflicts:
            details = "\n".join(
                f"{item.display_name}：{item.reason}"
                for item in conflicts
            )
            QMessageBox.warning(
                self,
                "账号冲突",
                "存在冲突，本次不会创建任何账号：\n" + details,
            )
            return
        pending = [
            item for item in candidates if item.status == "create"
        ]
        skipped = [
            item for item in candidates if item.status == "skip"
        ]
        if not pending:
            QMessageBox.information(
                self,
                "无需创建",
                f"扫描到 {len(candidates)} 个项目，"
                "对应运营账号均已存在。",
            )
            return
        preview = "\n".join(
            (
                f"{item.username}  {item.display_name}  "
                + "、".join(
                    f"{PROJECT_SCOPE_LABELS.get(scope, scope)}"
                    for scope, _name in item.project_access
                )
            )
            for item in pending[:20]
        )
        if len(pending) > 20:
            preview += f"\n……另有 {len(pending) - 20} 个"
        answer = QMessageBox.question(
            self,
            "确认创建运营账号",
            (
                f"扫描到 {len(candidates)} 个项目，"
                f"将新建 {len(pending)} 个运营账号，"
                f"跳过 {len(skipped)} 个已有账号。\n\n"
                f"{preview}\n\n"
                "默认密码为 123456，首次登录必须修改。"
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.repository.sync_operation_accounts_from_shared(
                self.actor
            )
        except Exception as exc:
            QMessageBox.warning(self, "创建运营账号失败", str(exc))
            return
        QMessageBox.information(
            self,
            "创建完成",
            (
                f"新建 {len(result.created)} 个，"
                f"跳过 {len(result.skipped)} 个。\n"
                + (
                    "新账号：" + "、".join(result.created)
                    if result.created
                    else ""
                )
            ),
        )
        self._refresh_users()

    def _edit_user(self) -> None:
        user = self._current_user()
        if user is None:
            QMessageBox.information(self, "未选择账号", "请先选择一个账号。")
            return
        try:
            menus = self.repository.user_menu_permissions(user.id)
            project_access = self.repository.user_project_access(user.id)
            shared_projects = self.repository.list_shared_projects()
        except Exception as exc:
            QMessageBox.warning(self, "读取权限失败", str(exc))
            return
        dialog = EditUserDialog(
            user,
            menus,
            project_access,
            shared_projects,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.repository.update_user(
                self.actor,
                user.id,
                username=dialog.username.text(),
                display_name=dialog.display_name.text(),
                role=dialog.role.currentData(),
                is_active=dialog.active.isChecked(),
                urgent_quota_per_day=dialog.urgent_quota.value(),
                max_generation_concurrency=dialog.max_concurrency.value(),
                menu_permissions=dialog.selected_menu_permissions(),
                project_access=dialog.selected_project_access(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "编辑失败", str(exc))
            return
        self._refresh_users()

    def _configure_api_key(self) -> None:
        user = self._current_user()
        if user is None:
            return
        if user.role not in (ROLE_AI, ROLE_ADMIN):
            QMessageBox.warning(
                self,
                "无需配置",
                "只有 AI 制作或管理员账号需要配置 API Key。",
            )
            return
        try:
            providers = tuple(
                self.repository.list_api_providers(
                    include_disabled=False
                )
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取 API 平台失败", str(exc))
            return
        if not providers:
            QMessageBox.warning(
                self,
                "没有可用平台",
                "请先在“API 平台”中启用至少一个平台。",
            )
            return
        dialog = ApiKeyDialog(
            self.repository,
            self.actor,
            user,
            providers,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_users()

    def _clear_api_key(self) -> None:
        user = self._current_user()
        if user is None:
            QMessageBox.information(self, "未选择账号", "请先选择一个账号。")
            return
        configured = [
            provider
            for provider in self.api_providers
            if (user.id, provider.id)
            in self.api_key_statuses_by_provider
        ]
        if not configured:
            QMessageBox.information(
                self,
                "无需清除",
                "当前账号尚未配置 API Key。",
            )
            return
        labels = [
            f"{provider.display_name}（尾号 "
            f"{self.api_key_statuses_by_provider[(user.id, provider.id)]['key_suffix']}）"
            for provider in configured
        ]
        selected, ok = QInputDialog.getItem(
            self,
            "清除 API Key",
            "选择要清除的平台：",
            labels,
            0,
            False,
        )
        if not ok:
            return
        provider = configured[labels.index(selected)]
        answer = QMessageBox.question(
            self,
            "清除 API Key",
            f"确定清除 {user.display_name} 在 {provider.display_name} 的 API Key 吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.clear_api_key(
                self.actor,
                user.id,
                provider_id=provider.id,
            )
        except Exception as exc:
            QMessageBox.warning(self, "清除失败", str(exc))
            return
        self._refresh_users()

    def _toggle_user(self) -> None:
        user = self._current_user()
        if user is None:
            return
        if user.id == self.actor.id:
            QMessageBox.warning(self, "不能停用", "不能停用当前登录账号。")
            return
        try:
            self.repository.set_user_active(
                self.actor,
                user.id,
                not user.is_active,
            )
        except Exception as exc:
            QMessageBox.warning(self, "操作失败", str(exc))
            return
        self._refresh_users()

    def _delete_user(self) -> None:
        user = self._current_user()
        if user is None:
            QMessageBox.information(self, "未选择账号", "请先选择一个账号。")
            return
        if user.id == self.actor.id:
            QMessageBox.warning(
                self,
                "不能删除",
                "不能删除当前登录账号。",
            )
            return
        answer = QMessageBox.warning(
            self,
            "删除账号",
            (
                f"确定删除账号“{user.display_name}”吗？\n"
                "删除后该账号无法登录，但任务和历史记录会保留。"
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.delete_user(self.actor, user.id)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self._refresh_users()

    def _reset_password(self) -> None:
        user = self._current_user()
        if user is None:
            return
        password, ok = QInputDialog.getText(
            self,
            "重置密码",
            f"为 {user.display_name} 设置临时密码：",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        try:
            self.repository.reset_password(self.actor, user.id, password)
        except Exception as exc:
            QMessageBox.warning(self, "重置失败", str(exc))
            return
        QMessageBox.information(
            self,
            "已重置",
            "临时密码已更新，用户下次登录必须修改密码。",
        )
        self._refresh_users()

    def _refresh_prices(self) -> None:
        try:
            prices = self.repository.list_model_prices()
        except Exception:
            prices = {}
        for model_id, spin in self.price_inputs.items():
            spin.setValue(float(prices.get(model_id, 0)))

    def _save_prices(self) -> None:
        try:
            for model_id, spin in self.price_inputs.items():
                self.repository.set_model_price(
                    self.actor,
                    model_id,
                    Decimal(str(spin.value())),
                )
        except Exception as exc:
            QMessageBox.warning(self, "保存价格失败", str(exc))
            return
        QMessageBox.information(self, "已保存", "新价格将用于后续生成记录。")

    def _refresh_settings(self) -> None:
        try:
            value = self.repository.get_shared_setting("share_root")
        except Exception:
            value = None
        self.share_root_edit.setText(value or "")
        root = Path(value) if value else None
        self.internal_project_label.setText(
            str(root / "内部项目") if root else "未配置"
        )
        self.external_project_label.setText(
            str(root / "外部项目") if root else "未配置"
        )

    def _scan_shared_projects(self) -> None:
        try:
            projects = self.repository.list_shared_projects()
        except Exception as exc:
            QMessageBox.warning(self, "扫描失败", str(exc))
            return
        QMessageBox.information(
            self,
            "扫描完成",
            f"内部项目 {len(projects.get('internal', ()))} 个，"
            f"外部项目 {len(projects.get('external', ()))} 个。"
            "可在“编辑账号”中分配项目。",
        )

    def _save_share_root(self) -> None:
        value = self.share_root_edit.text().strip()
        if not value:
            QMessageBox.warning(self, "不能为空", "请填写共享盘 UNC 根目录。")
            return
        if value.startswith("//"):
            value = "\\\\" + value[2:].replace("/", "\\")
        if not value.startswith("\\\\") or "\\" not in value[2:]:
            QMessageBox.warning(
                self,
                "路径格式不正确",
                "请填写类似 \\\\服务器\\共享目录 的 UNC 路径。",
            )
            return
        try:
            self.repository.set_shared_setting(
                self.actor,
                "share_root",
                value,
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        QMessageBox.information(self, "已保存", "共享盘根目录已更新。")

    def _test_database(self) -> None:
        try:
            result = self.repository.test_connection()
        except Exception as exc:
            QMessageBox.warning(self, "数据库连接失败", str(exc))
            return
        QMessageBox.information(
            self,
            "数据库连接正常",
            f"数据库：{result['db']}\nMySQL：{result['version']}",
        )

    def _refresh_templates(self) -> None:
        try:
            users = self.repository.list_templates(self.actor)
        except Exception:
            users = []
        shared = [item for item in users if item["scope"] == "shared"]
        self.template_list.clear()
        for template in shared:
            item = QListWidgetItem(template["name"])
            item.setData(Qt.ItemDataRole.UserRole, template)
            self.template_list.addItem(item)

    def _current_template(self) -> dict | None:
        item = self.template_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _add_template(self) -> None:
        name, ok = QInputDialog.getText(self, "新增共享模板", "模板名称")
        if not ok or not name.strip():
            return
        prompt, ok = QInputDialog.getMultiLineText(
            self,
            "新增共享模板",
            "提示词",
        )
        if not ok or not prompt.strip():
            return
        try:
            self.repository.save_template(
                self.actor,
                scope="shared",
                name=name,
                prompt=prompt,
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._refresh_templates()

    def _edit_template(self) -> None:
        template = self._current_template()
        if template is None:
            return
        name, ok = QInputDialog.getText(
            self,
            "编辑共享模板",
            "模板名称",
            text=template["name"],
        )
        if not ok or not name.strip():
            return
        prompt, ok = QInputDialog.getMultiLineText(
            self,
            "编辑共享模板",
            "提示词",
            template["prompt"],
        )
        if not ok or not prompt.strip():
            return
        try:
            self.repository.save_template(
                self.actor,
                scope="shared",
                name=name,
                prompt=prompt,
                template_id=template["id"],
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._refresh_templates()

    def _delete_template(self) -> None:
        template = self._current_template()
        if template is None:
            return
        answer = QMessageBox.question(
            self,
            "删除共享模板",
            f"确定删除“{template['name']}”吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.delete_template(template["id"], self.actor)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self._refresh_templates()

    def _refresh_projects(self) -> None:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        self._exception_cutoff = cutoff
        if not self.isVisible():
            try:
                projects = self.repository.list_exception_projects(
                    cutoff
                )
            except Exception as exc:
                QMessageBox.warning(self, "读取任务失败", str(exc))
                return
            self._render_exception_projects(projects)
            return
        self._active_exception_request = self.query_runner.submit(
            lambda: self.repository.list_exception_projects(cutoff)
        )

    def _query_succeeded(self, request_id: int, result) -> None:
        if request_id != self._active_exception_request:
            return
        self._render_exception_projects(result)

    def _render_exception_projects(self, result) -> None:
        self.projects = list(result)
        self.project_table.setRowCount(0)
        for project in self.projects:
            row = self.project_table.rowCount()
            self.project_table.insertRow(row)
            values = (
                project.project_code,
                project.product_code or project.name,
                project.owner_name,
                PROJECT_STATUS_LABELS.get(project.status, project.status),
                f"{project.approved_count}/{project.task_count}",
                "是" if project.is_urgent else "否",
                project.source_date.isoformat() if project.source_date else "",
            )
            for column, value in enumerate(values):
                self.project_table.setItem(row, column, QTableWidgetItem(value))
        if self.projects:
            self.project_table.selectRow(0)

    def _query_failed(self, request_id: int, message: str) -> None:
        if request_id != self._active_exception_request:
            return
        QMessageBox.warning(self, "读取任务失败", message)

    def _current_admin_project(self) -> ProjectRecord | None:
        row = self.project_table.currentRow()
        if not 0 <= row < len(self.projects):
            return None
        return self.projects[row]

    def _admin_project_selected(self) -> None:
        project = self._current_admin_project()
        if project is None:
            return
        cutoff = datetime.utcnow() - timedelta(hours=24)
        try:
            self.tasks = (
                self.repository.list_project_tasks(project.id)
                if project.status == "cancelled"
                else self.repository.list_exception_tasks(
                    project.id,
                    cutoff,
                )
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            return
        self.admin_task_table.setRowCount(0)
        for task in self.tasks:
            row = self.admin_task_table.rowCount()
            self.admin_task_table.insertRow(row)
            values = (
                task.task_code,
                task.garment_name,
                task.model_name,
                task.assignee_name or "未分配",
                TASK_STATUS_LABELS.get(task.status, task.status),
                str(task.final_quantity),
            )
            for column, value in enumerate(values):
                self.admin_task_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )

    def _task_needs_admin_attention(
        self,
        task: ProjectTask,
        cutoff: datetime,
    ) -> bool:
        if task.status == "returned":
            return True
        if task.status == "available" and task.created_at < cutoff:
            return True
        if (
            task.status == "working"
            and (task.updated_at or task.work_started_at or task.created_at)
            < cutoff
        ):
            return True
        try:
            attempts = self.repository.list_attempts(task.id)
        except Exception:
            attempts = []
        if not attempts:
            return False
        latest = attempts[0]
        return bool(
            latest.status == "failed"
            and latest.created_at is not None
            and latest.created_at >= cutoff
        )

    def _current_admin_task(self) -> ProjectTask | None:
        row = self.admin_task_table.currentRow()
        if not 0 <= row < len(self.tasks):
            return None
        return self.tasks[row]

    def _reassign_task(self) -> None:
        task = self._current_admin_task()
        if task is None:
            return
        ai_users = [user for user in self.users if user.role == ROLE_AI and user.is_active]
        if not ai_users:
            QMessageBox.warning(self, "没有可用人员", "请先创建并启用 AI 制作账号。")
            return
        labels = [f"{user.display_name}（{user.username}）" for user in ai_users]
        label, ok = QInputDialog.getItem(
            self,
            "改派任务",
            "目标 AI 人员",
            labels,
            0,
            False,
        )
        if not ok:
            return
        target = ai_users[labels.index(label)]
        try:
            self.repository.reassign_task(task.id, self.actor, target.id)
        except Exception as exc:
            QMessageBox.warning(self, "改派失败", str(exc))
            return
        self._admin_project_selected()

    def _admin_cancel_project(self) -> None:
        project = self._current_admin_project()
        if project is None:
            return
        answer = QMessageBox.question(
            self,
            "取消任务",
            f"确定取消任务“{project.project_code}”吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.cancel_project(project.id, self.actor)
        except Exception as exc:
            QMessageBox.warning(self, "取消失败", str(exc))
            return
        self._refresh_projects()

    def _reopen_project(self) -> None:
        project = self._current_admin_project()
        if project is None:
            return
        reason, ok = QInputDialog.getMultiLineText(
            self,
            "重开任务",
            "重开原因",
        )
        if not ok or not reason.strip():
            return
        try:
            self.repository.reopen_project(project.id, self.actor, reason)
        except Exception as exc:
            QMessageBox.warning(self, "重开失败", str(exc))
            return
        self._refresh_projects()
