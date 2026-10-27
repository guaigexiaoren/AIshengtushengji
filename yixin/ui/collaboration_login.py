from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.repository import MySQLCentralRepository
from ..collaboration.types import AuthUser


class ChangePasswordDialog(QDialog):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        user: AuthUser,
        old_password: str,
        *,
        forced: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.user = user
        self.old_password = old_password
        self.setWindowTitle("修改密码")
        layout = QVBoxLayout(self)
        if forced:
            label = QLabel("首次登录必须先修改临时密码。")
            label.setStyleSheet("color:#b25a24;")
            layout.addWidget(label)
        form = QFormLayout()
        self.new_password = QLineEdit()
        self.new_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_password = QLineEdit()
        self.confirm_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("新密码", self.new_password)
        form.addRow("确认新密码", self.confirm_password)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        if self.new_password.text() != self.confirm_password.text():
            QMessageBox.warning(self, "密码不一致", "两次输入的新密码不一致。")
            return
        try:
            self.repository.change_password(
                self.user.id,
                self.old_password,
                self.new_password.text(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "修改失败", str(exc))
            return
        self.accept()


class InitialAdminDialog(QDialog):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.setWindowTitle("创建首个管理员")
        layout = QVBoxLayout(self)
        note = QLabel(
            "数据库中还没有账号。创建后可用该账号登录，首次登录必须修改密码。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.username = QLineEdit("admin")
        self.display_name = QLineEdit("系统管理员")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_password = QLineEdit()
        self.confirm_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("管理员账号", self.username)
        form.addRow("姓名", self.display_name)
        form.addRow("登录密码", self.password)
        form.addRow("确认密码", self.confirm_password)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _create(self) -> None:
        if self.password.text() != self.confirm_password.text():
            QMessageBox.warning(self, "密码不一致", "两次输入的密码不一致。")
            return
        try:
            self.repository.create_initial_admin(
                self.username.text().strip(),
                self.display_name.text().strip(),
                self.password.text(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "创建失败", str(exc))
            return
        self.accept()


class LoginDialog(QDialog):
    def __init__(
        self,
        repository: MySQLCentralRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.user: AuthUser | None = None
        self.setWindowTitle("登录亦欣 AI 生图工作台")
        self.resize(440, 300)
        layout = QVBoxLayout(self)
        brand = QLabel("亦欣 AI 生图工作台")
        brand.setStyleSheet("font-size:24px;font-weight:700;color:#1f2d43;")
        subtitle = QLabel("运营 · AI 制作 · 管理员")
        subtitle.setStyleSheet("color:#6d7a91;")
        layout.addWidget(brand)
        layout.addWidget(subtitle)
        form = QFormLayout()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("账号", self.username)
        form.addRow("密码", self.password)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.login_button = QPushButton("登录")
        self.login_button.setObjectName("PrimaryButton")
        self.login_button.setAutoDefault(True)
        self.login_button.setDefault(True)
        self.login_button.clicked.connect(self._login)
        self.cancel_button = QPushButton("退出")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.login_button)
        layout.addLayout(actions)

    def _login(self) -> None:
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            QMessageBox.warning(self, "信息不完整", "请输入账号和密码。")
            self.password.setFocus()
            return
        try:
            user = self.repository.authenticate_user(username, password)
        except Exception as exc:
            QMessageBox.warning(self, "登录失败", str(exc))
            self.password.clear()
            self.password.setFocus()
            return
        if user.must_change_password:
            dialog = ChangePasswordDialog(
                self.repository,
                user,
                password,
                forced=True,
                parent=self,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            user = AuthUser(
                id=user.id,
                username=user.username,
                display_name=user.display_name,
                role=user.role,
                must_change_password=False,
                is_active=True,
                urgent_quota_per_day=user.urgent_quota_per_day,
                max_generation_concurrency=user.max_generation_concurrency,
            )
        self.user = user
        self.accept()
