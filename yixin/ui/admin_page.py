from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
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

from ..collaboration.reports import ReportData, ReportFilter, export_report_excel
from ..collaboration.reports import GroupMetrics
from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.types import (
    PROJECT_STATUS_LABELS,
    ROLE_ADMIN,
    ROLE_AI,
    ROLE_LABELS,
    ROLE_OPERATIONS,
    TASK_STATUS_LABELS,
    AuthUser,
    MENU_KEYS,
    PROJECT_SCOPE_LABELS,
    PROJECT_SCOPES,
    ROLE_ALLOWED_MENUS,
    ROLE_DEFAULT_MENUS,
    ProjectRecord,
    ProjectTask,
)
from ..constants import MODEL_OPTIONS
from ..security import validate_api_key
from ..time_utils import utc_naive_to_local


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
        self.password.setPlaceholderText("至少 8 个字符，首次登录后强制修改")
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
        actions = QHBoxLayout()
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        save.setObjectName("PrimaryButton")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)


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

        actions = QHBoxLayout()
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        save.setObjectName("PrimaryButton")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)
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
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("粘贴新的 API Key")
        form.addRow("API Key", self.key_edit)
        layout.addLayout(form)
        actions = QHBoxLayout()
        check = QPushButton("检查格式")
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        save.setObjectName("PrimaryButton")
        cancel.clicked.connect(self.reject)
        check.clicked.connect(self._check_format)
        save.clicked.connect(self._save)
        actions.addStretch(1)
        actions.addWidget(check)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def _check_format(self) -> None:
        try:
            validate_api_key(self.key_edit.text())
        except Exception as exc:
            QMessageBox.warning(self, "格式不正确", str(exc))
            return
        QMessageBox.information(self, "格式正确", "API Key 格式检查通过。")

    def _save(self) -> None:
        try:
            self.repository.save_api_key(
                self.actor,
                self.target.id,
                self.key_edit.text(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()


class AdminTrendChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.groups: tuple[GroupMetrics, ...] = ()
        self.setMinimumHeight(180)

    def set_groups(self, groups: tuple[GroupMetrics, ...]) -> None:
        self.groups = groups
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f7f9fd"))
        available = self.rect().adjusted(48, 28, -18, -34)
        painter.setFont(QFont(self.font().family(), 9))
        painter.setPen(QPen(QColor("#8795aa"), 1))
        painter.drawText(16, 20, "每日任务量趋势")
        painter.drawLine(
            available.left(),
            available.bottom(),
            available.right(),
            available.bottom(),
        )
        if not self.groups:
            painter.setPen(QColor("#7d8ba0"))
            painter.drawText(
                available,
                Qt.AlignmentFlag.AlignCenter,
                "当前筛选范围暂无数据",
            )
            return
        maximum = max(group.task_count for group in self.groups) or 1
        slot_width = available.width() / max(1, len(self.groups))
        bar_width = max(8.0, min(48.0, slot_width * 0.55))
        for index, group in enumerate(self.groups):
            center = available.left() + slot_width * (index + 0.5)
            height = available.height() * group.task_count / maximum
            bar = (
                center - bar_width / 2,
                available.bottom() - height,
                bar_width,
                height,
            )
            painter.fillRect(
                int(bar[0]),
                int(bar[1]),
                int(bar[2]),
                max(1, int(bar[3])),
                QColor("#4778cc"),
            )
            painter.setPen(QColor("#4f5d72"))
            painter.drawText(
                int(center - slot_width / 2),
                available.bottom() + 6,
                int(slot_width),
                18,
                Qt.AlignmentFlag.AlignHCenter,
                group.label[5:] if len(group.label) >= 10 else group.label,
            )
            painter.drawText(
                int(center - slot_width / 2),
                int(bar[1] - 18),
                int(slot_width),
                16,
                Qt.AlignmentFlag.AlignHCenter,
                str(group.task_count),
            )


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
        self.report_data: ReportData | None = None
        self._build()
        if auto_load:
            self.refresh_all()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("管理员控制台")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel("账号、模型成本、共享设置、异常处理和经营报表。")
        subtitle.setStyleSheet("color:#6d7a91;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_users_tab(), "用户与权限")
        self.tabs.addTab(self._build_settings_tab(), "价格与共享设置")
        self.tabs.addTab(self._build_exceptions_tab(), "任务异常")
        self.tabs.addTab(self._build_reports_tab(), "经营报表")
        layout.addWidget(self.tabs, 1)

    def refresh_all(self) -> None:
        self._refresh_users()
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
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self._refresh_users)
        toolbar.addWidget(add_button)
        toolbar.addWidget(edit_button)
        toolbar.addWidget(self.toggle_user_button)
        toolbar.addWidget(reset_button)
        toolbar.addWidget(self.api_key_button)
        toolbar.addWidget(self.clear_api_key_button)
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
        actions = QHBoxLayout()
        reassign = QPushButton("改派所选任务")
        cancel = QPushButton("取消所选任务")
        reopen = QPushButton("重开所选任务")
        refresh = QPushButton("刷新")
        reassign.clicked.connect(self._reassign_task)
        cancel.clicked.connect(self._admin_cancel_project)
        reopen.clicked.connect(self._reopen_project)
        refresh.clicked.connect(self._refresh_projects)
        actions.addWidget(reassign)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(reopen)
        actions.addWidget(refresh)
        layout.addLayout(actions)
        return widget

    def _build_reports_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        filters = QHBoxLayout()
        self.start_date = QDateEdit(QDate.currentDate().addDays(-30))
        self.start_date.setCalendarPopup(True)
        self.end_date = QDateEdit(QDate.currentDate())
        self.end_date.setCalendarPopup(True)
        self.project_filter = QComboBox()
        self.user_filter = QComboBox()
        self.model_filter = QComboBox()
        self.model_filter.addItem("全部模型", "")
        for label, model_id in MODEL_OPTIONS:
            self.model_filter.addItem(label, model_id)
        search = QPushButton("生成报表")
        search.clicked.connect(self._load_report)
        export = QPushButton("导出 Excel")
        export.clicked.connect(self._export_report)
        filters.addWidget(QLabel("开始"))
        filters.addWidget(self.start_date)
        filters.addWidget(QLabel("结束"))
        filters.addWidget(self.end_date)
        filters.addWidget(self.project_filter)
        filters.addWidget(self.user_filter)
        filters.addWidget(self.model_filter)
        filters.addStretch(1)
        filters.addWidget(search)
        filters.addWidget(export)
        layout.addLayout(filters)
        self.summary_label = QLabel("尚未生成报表")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(
            "background:#eef4ff;border:1px solid #d6e2f8;"
            "border-radius:8px;padding:12px;line-height:1.7;"
        )
        layout.addWidget(self.summary_label)
        self.trend_chart = AdminTrendChart()
        layout.addWidget(self.trend_chart)
        self.report_table = QTableWidget(0, 9)
        self.report_table.setHorizontalHeaderLabels(
            [
                "任务编码",
                "子任务",
                "制作人员",
                "模型",
                "服装",
                "状态",
                "最终图片",
                "返工",
                "成本",
            ]
        )
        self.report_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.report_table.horizontalHeader().setStretchLastSection(True)
        self.report_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate(
            (220, 220, 120, 130, 150, 100, 80, 70, 90)
        ):
            self.report_table.setColumnWidth(column, width)
        layout.addWidget(self.report_table, 1)
        return widget

    def _refresh_users(self) -> None:
        try:
            self.users = self.repository.list_users()
            self.api_key_statuses = self.repository.api_key_statuses()
        except Exception as exc:
            QMessageBox.warning(self, "读取账号失败", str(exc))
            return
        self.user_table.setRowCount(0)
        for user in self.users:
            row = self.user_table.rowCount()
            self.user_table.insertRow(row)
            values = (
                user.username,
                user.display_name,
                ROLE_LABELS.get(user.role, user.role),
                "启用" if user.is_active else "停用",
                "是" if user.must_change_password else "否",
                (
                    f"已配置 · {key_info['key_suffix']}"
                    if (key_info := self.api_key_statuses.get(user.id))
                    else "未配置"
                ),
                (
                    utc_naive_to_local(key_info["updated_at"]).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if (key_info := self.api_key_statuses.get(user.id))
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
        self._refresh_filter_users()

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
        dialog = ApiKeyDialog(self.repository, self.actor, user, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_users()

    def _clear_api_key(self) -> None:
        user = self._current_user()
        if user is None or user.id not in self.api_key_statuses:
            return
        answer = QMessageBox.question(
            self,
            "清除 API Key",
            f"确定清除 {user.display_name} 的 API Key 吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.clear_api_key(self.actor, user.id)
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
            self.repository.set_user_active(user.id, not user.is_active)
        except Exception as exc:
            QMessageBox.warning(self, "操作失败", str(exc))
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
            self.repository.reset_password(user.id, password)
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
        try:
            all_projects = self.repository.list_projects()
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            return
        self.projects = []
        cutoff = datetime.utcnow() - timedelta(hours=24)
        for project in all_projects:
            try:
                tasks = self.repository.list_project_tasks(project.id)
            except Exception:
                tasks = []
            exceptional = [
                task
                for task in tasks
                if self._task_needs_admin_attention(task, cutoff)
            ]
            if project.status == "cancelled" or exceptional:
                self.projects.append(project)
        self.project_table.setRowCount(0)
        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem("全部任务", None)
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
            self.project_filter.addItem(
                f"{project.project_code} · {project.product_code or project.name}",
                project.id,
            )
        self.project_filter.blockSignals(False)
        if self.projects:
            self.project_table.selectRow(0)

    def _current_admin_project(self) -> ProjectRecord | None:
        row = self.project_table.currentRow()
        if not 0 <= row < len(self.projects):
            return None
        return self.projects[row]

    def _admin_project_selected(self) -> None:
        project = self._current_admin_project()
        if project is None:
            return
        try:
            self.tasks = self.repository.list_project_tasks(project.id)
        except Exception as exc:
            QMessageBox.warning(self, "读取任务失败", str(exc))
            return
        if project.status != "cancelled":
            cutoff = datetime.utcnow() - timedelta(hours=24)
            self.tasks = [
                task
                for task in self.tasks
                if self._task_needs_admin_attention(task, cutoff)
            ]
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

    def _refresh_filter_users(self) -> None:
        if not hasattr(self, "user_filter"):
            return
        current = self.user_filter.currentData()
        self.user_filter.clear()
        self.user_filter.addItem("全部人员", None)
        for user in self.users:
            self.user_filter.addItem(user.display_name, user.id)
        if current is not None:
            index = self.user_filter.findData(current)
            if index >= 0:
                self.user_filter.setCurrentIndex(index)

    def _load_report(self) -> None:
        if self.start_date.date() > self.end_date.date():
            QMessageBox.warning(self, "日期范围错误", "开始日期不能晚于结束日期。")
            return
        report_filter = ReportFilter(
            start_date=self.start_date.date().toPython(),
            end_date=self.end_date.date().toPython(),
            project_id=self.project_filter.currentData(),
            assignee_id=self.user_filter.currentData(),
            model=self.model_filter.currentData() or "",
        )
        self.report_data = None
        try:
            self.report_data = self.repository.report(report_filter)
        except Exception as exc:
            QMessageBox.warning(self, "报表生成失败", str(exc))
            return
        summary = self.report_data.summary
        self.summary_label.setText(
            f"总任务 {summary.project_count} 个　子任务 {summary.task_count} 个　"
            f"已通过 {summary.approved_task_count} 个　"
            f"总任务完成率 {summary.project_completion_rate:.1%}　"
            f"子任务通过率 {summary.task_completion_rate:.1%}　"
            f"最终图片 {summary.final_image_count} 张　"
            f"返工 {summary.rework_count} 次（{summary.rework_rate:.1%}）\n"
            f"生成图片 {summary.generated_image_count} 张　"
            f"PS成品 {summary.manual_image_count} 张　"
            f"成本 ¥{summary.total_cost:.2f}　"
            f"每通过任务 ¥{summary.cost_per_approved_task:.2f}　"
            f"每最终图 ¥{summary.cost_per_final_image:.2f}　"
            f"平均制作 {summary.average_production_seconds / 3600:.2f} 小时　"
            f"平均审核 {summary.average_review_seconds / 3600:.2f} 小时　"
            f"端到端 {summary.average_end_to_end_seconds / 3600:.2f} 小时"
        )
        self.trend_chart.set_groups(self.report_data.by_date)
        self.report_table.setRowCount(0)
        for item in self.report_data.rows:
            row = self.report_table.rowCount()
            self.report_table.insertRow(row)
            values = (
                item.project_code,
                item.task_code,
                item.assignee_name or "未分配",
                item.model,
                item.garment_name,
                TASK_STATUS_LABELS.get(item.task_status, item.task_status),
                str(item.final_quantity),
                str(item.rework_count),
                f"¥{item.generation_cost:.2f}",
            )
            for column, value in enumerate(values):
                self.report_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )

    def _export_report(self) -> None:
        if self.report_data is None:
            self._load_report()
        if self.report_data is None:
            return
        default_name = f"经营报表_{date.today():%Y%m%d}.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出经营报表",
            default_name,
            "Excel 工作簿 (*.xlsx)",
        )
        if not path:
            return
        try:
            export_report_excel(self.report_data, Path(path))
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "已导出", path)
