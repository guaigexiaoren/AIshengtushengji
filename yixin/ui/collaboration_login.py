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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..collaboration.repository import MySQLCentralRepository, MySQLConfig
from ..collaboration.types import AuthUser
from ..security import EncryptedJsonStore


class DatabaseConfigDialog(QDialog):
    def __init__(
        self,
        store: EncryptedJsonStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.saved_config: MySQLConfig | None = None
        existing = store.load() or {}
        self.setWindowTitle("内网数据库设置")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        note = QLabel(
            "每台电脑只需配置一次。密码使用当前 Windows 账号的 DPAPI 加密，"
            "不会写入日志或项目代码。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#60708a;")
        layout.addWidget(note)
        form = QFormLayout()
        self.host = QLineEdit(str(existing.get("host", "")))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(existing.get("port", 3306)))
        self.database = QLineEdit(str(existing.get("database", "")))
        self.user = QLineEdit(str(existing.get("user", "")))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(
            "留空表示继续使用已经加密保存的密码"
            if existing.get("password")
            else "数据库密码"
        )
        form.addRow("服务器", self.host)
        form.addRow("端口", self.port)
        form.addRow("数据库", self.database)
        form.addRow("账号", self.user)
        form.addRow("密码", self.password)
        layout.addLayout(form)
        actions = QHBoxLayout()
        test_button = QPushButton("测试连接")
        init_button = QPushButton("初始化/升级表结构")
        test_button.clicked.connect(self._test)
        init_button.clicked.connect(self._initialize)
        actions.addWidget(test_button)
        actions.addWidget(init_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _config(self) -> MySQLConfig:
        password = self.password.text()
        if not password:
            existing = self.store.load() or {}
            password = str(existing.get("password", ""))
        return MySQLConfig(
            host=self.host.text().strip(),
            port=self.port.value(),
            database=self.database.text().strip(),
            user=self.user.text().strip(),
            password=password,
        )

    def _validate_fields(self) -> bool:
        fields = (self.host, self.database, self.user)
        if any(not field.text().strip() for field in fields):
            QMessageBox.warning(self, "信息不完整", "服务器、数据库和账号不能为空。")
            return False
        if not self.password.text() and not (self.store.load() or {}).get("password"):
            QMessageBox.warning(self, "缺少密码", "请输入数据库密码。")
            return False
        return True

    def _test(self) -> None:
        if not self._validate_fields():
            return
        try:
            result = MySQLCentralRepository(self._config()).test_connection()
        except Exception as exc:
            QMessageBox.warning(self, "连接失败", str(exc))
            return
        QMessageBox.information(
            self,
            "连接成功",
            f"数据库：{result['db']}\nMySQL：{result['version']}",
        )

    def _initialize(self) -> None:
        if not self._validate_fields():
            return
        try:
            version = MySQLCentralRepository(self._config()).ensure_schema()
        except Exception as exc:
            QMessageBox.warning(self, "初始化失败", str(exc))
            return
        QMessageBox.information(
            self,
            "初始化完成",
            f"协同表结构版本：{version}\n"
            "如数据库中还没有管理员，请运行 tools/init_mysql.py。",
        )

    def _save(self) -> None:
        if not self._validate_fields():
            return
        config = self._config()
        try:
            self.store.save(
                {
                    "host": config.host,
                    "port": config.port,
                    "database": config.database,
                    "user": config.user,
                    "password": config.password,
                    "charset": config.charset,
                }
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.saved_config = config
        self.accept()


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
        config_store: EncryptedJsonStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.config_store = config_store
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
        db_button = QPushButton("数据库设置")
        db_button.clicked.connect(self._open_database_settings)
        login_button = QPushButton("登录")
        login_button.setObjectName("PrimaryButton")
        login_button.clicked.connect(self._login)
        cancel_button = QPushButton("退出")
        cancel_button.clicked.connect(self.reject)
        actions.addWidget(db_button)
        actions.addStretch(1)
        actions.addWidget(cancel_button)
        actions.addWidget(login_button)
        layout.addLayout(actions)
        self.password.returnPressed.connect(self._login)

    def _open_database_settings(self) -> None:
        dialog = DatabaseConfigDialog(self.config_store, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.saved_config:
            self.repository = MySQLCentralRepository(dialog.saved_config)

    def _login(self) -> None:
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            QMessageBox.warning(self, "信息不完整", "请输入账号和密码。")
            return
        try:
            user = self.repository.authenticate_user(username, password)
        except Exception as exc:
            QMessageBox.warning(self, "登录失败", str(exc))
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
            )
        self.user = user
        self.accept()
