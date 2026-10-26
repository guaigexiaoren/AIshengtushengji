from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
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
    PRIORITY_LABELS,
    PROJECT_STATUS_LABELS,
    ROLE_ADMIN,
    ROLE_AI,
    ROLE_LABELS,
    ROLE_OPERATIONS,
    TASK_STATUS_LABELS,
    AuthUser,
    ProjectRecord,
    ProjectTask,
)
from ..constants import MODEL_OPTIONS
from ..time_utils import utc_naive_to_local


class AddUserDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新增账号")
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
        form.addRow("登录账号", self.username)
        form.addRow("姓名", self.display_name)
        form.addRow("角色", self.role)
        form.addRow("临时密码", self.password)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


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
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.actor = actor
        self.projects: list[ProjectRecord] = []
        self.tasks: list[ProjectTask] = []
        self.users: list[AuthUser] = []
        self.report_data: ReportData | None = None
        self._build()
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
        self.tabs.addTab(self._build_exceptions_tab(), "项目异常")
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
        self.toggle_user_button = QPushButton("停用/启用")
        self.toggle_user_button.clicked.connect(self._toggle_user)
        reset_button = QPushButton("重置密码")
        reset_button.clicked.connect(self._reset_password)
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self._refresh_users)
        toolbar.addWidget(add_button)
        toolbar.addWidget(self.toggle_user_button)
        toolbar.addWidget(reset_button)
        toolbar.addStretch(1)
        toolbar.addWidget(refresh_button)
        layout.addLayout(toolbar)
        self.user_table = QTableWidget(0, 5)
        self.user_table.setHorizontalHeaderLabels(
            ["账号", "姓名", "角色", "状态", "首次改密"]
        )
        self.user_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.user_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.user_table.horizontalHeader().setStretchLastSection(True)
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
        share_actions = QHBoxLayout()
        save_share = QPushButton("保存共享目录")
        save_share.clicked.connect(self._save_share_root)
        test_connection = QPushButton("测试数据库连接")
        test_connection.clicked.connect(self._test_database)
        share_actions.addWidget(save_share)
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
            ["项目编号", "名称", "运营", "状态", "任务进度", "优先级", "截止时间"]
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
        splitter.addWidget(self.admin_task_table)
        layout.addWidget(splitter, 1)
        actions = QHBoxLayout()
        reassign = QPushButton("改派所选任务")
        cancel = QPushButton("取消所选项目")
        reopen = QPushButton("重开所选项目")
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
                "项目",
                "任务",
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
        layout.addWidget(self.report_table, 1)
        return widget

    def _refresh_users(self) -> None:
        try:
            self.users = self.repository.list_users()
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
        dialog = AddUserDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.repository.create_user(
                dialog.username.text(),
                dialog.display_name.text(),
                dialog.role.currentData(),
                dialog.password.text(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "新增失败", str(exc))
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
            self.projects = self.repository.list_projects()
        except Exception as exc:
            QMessageBox.warning(self, "读取项目失败", str(exc))
            return
        self.project_table.setRowCount(0)
        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem("全部项目", None)
        for project in self.projects:
            row = self.project_table.rowCount()
            self.project_table.insertRow(row)
            values = (
                project.project_code,
                project.name,
                project.owner_name,
                PROJECT_STATUS_LABELS.get(project.status, project.status),
                f"{project.approved_count}/{project.task_count}",
                PRIORITY_LABELS.get(project.priority, project.priority),
                utc_naive_to_local(project.deadline).strftime("%Y-%m-%d %H:%M"),
            )
            for column, value in enumerate(values):
                self.project_table.setItem(row, column, QTableWidgetItem(value))
            self.project_filter.addItem(
                f"{project.project_code} · {project.name}",
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
            "取消项目",
            f"确定取消“{project.project_code}”吗？",
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
            "重开项目",
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
            f"项目 {summary.project_count} 个　任务 {summary.task_count} 个　"
            f"已通过 {summary.approved_task_count} 个　"
            f"项目完成率 {summary.project_completion_rate:.1%}　"
            f"任务通过率 {summary.task_completion_rate:.1%}　"
            f"最终图片 {summary.final_image_count} 张　"
            f"返工 {summary.rework_count} 次（{summary.rework_rate:.1%}）\n"
            f"生成图片 {summary.generated_image_count} 张　"
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
