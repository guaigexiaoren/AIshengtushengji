from __future__ import annotations

import ctypes
import os
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QTimer, QTranslator
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .api import NanoBananaClient
from .collaboration.cleanup import cleanup_expired_candidates
from .collaboration.repository import (
    MySQLCentralRepository,
    MySQLConfig,
    SchemaNotInitializedError,
)
from .collaboration.shared_paths import SharedPathResolver
from .collaboration.upgrade import prepare_legacy_data
from .constants import APP_NAME, APP_ORGANIZATION, TEMP_RETENTION_DAYS
from .database import Database
from .library import LibraryService
from .logging_utils import register_sensitive_value, setup_logging
from .paths import AppPaths, resource_path
from .security import EncryptedJsonStore, SecretStore
from .storage import FileStore
from .ui.main_window import MainWindow
from .ui.collaboration_login import (
    DatabaseConfigDialog,
    InitialAdminDialog,
    LoginDialog,
)
from .ui.theme import APP_STYLESHEET
from .worker import TaskWorker


class StartupCancelled(Exception):
    pass


def run() -> int:
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Yixin.AIImageWorkbench.1"
            )
        except Exception:
            pass
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORGANIZATION)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLESHEET)
    app.setWindowIcon(QIcon(str(resource_path("assets", "app_icon.ico"))))
    translator = QTranslator(app)
    translations_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load("qt_zh_CN", translations_path):
        app.installTranslator(translator)
        app.translator = translator

    paths = AppPaths.default()
    try:
        paths.ensure()
        prepare_legacy_data(paths)
        logger = setup_logging(paths.log_file)
        logger.info("应用启动，数据目录：%s", paths.root)
        database = Database(paths.database)
        database.initialize()
        cancelled = database.recover_after_restart()
        if cancelled:
            logger.info("已将 %s 个用户停止等待的任务恢复为已取消", cancelled)
        file_store = FileStore(paths)
        file_store.cleanup_temp(
            database.active_temp_paths(),
            days=TEMP_RETENTION_DAYS,
        )
        secret_store = SecretStore(paths.api_key_file)
        register_sensitive_value(secret_store.load())
        client = NanoBananaClient(lambda: secret_store.load())
        database_config_store = EncryptedJsonStore(paths.database_config_file)
        database_config = _load_database_config(database_config_store)
        repository = MySQLCentralRepository(database_config)
        _ensure_central_schema(repository)
        _ensure_initial_admin(repository)
        login = LoginDialog(repository, database_config_store)
        if login.exec() != login.DialogCode.Accepted or login.user is None:
            return 0
        actor = login.user
        repository = login.repository
        shared_root = repository.get_shared_setting("share_root")
        if not shared_root:
            QMessageBox.warning(
                None,
                "尚未配置共享盘",
                "管理员需要先在“管理控制台 → 价格与共享设置”中"
                "填写统一 UNC 共享盘根目录。",
            )
        resolver = SharedPathResolver(Path(shared_root or Path.cwd()))
        worker = TaskWorker(database, client, file_store)
        library_service = LibraryService(database, file_store, paths)
        window = MainWindow(
            paths=paths,
            database=database,
            file_store=file_store,
            library_service=library_service,
            secret_store=secret_store,
            worker=worker,
            client=client,
            repository=repository,
            resolver=resolver,
            actor=actor,
        )
    except StartupCancelled:
        return 0
    except Exception as exc:
        QMessageBox.critical(None, "启动失败", f"程序无法启动：\n{exc}")
        return 1

    def cleanup_temp() -> None:
        try:
            file_store.cleanup_temp(
                database.active_temp_paths(),
                days=TEMP_RETENTION_DAYS,
            )
        except Exception:
            logger.exception("自动清理临时文件失败")
        _cleanup_collaboration_candidates(repository, resolver)

    cleanup_timer = QTimer(window)
    cleanup_timer.setInterval(24 * 60 * 60 * 1000)
    cleanup_timer.timeout.connect(cleanup_temp)
    cleanup_timer.start()
    app.aboutToQuit.connect(worker.shutdown)

    window.show()
    worker.start()
    QTimer.singleShot(
        0,
        lambda: _cleanup_collaboration_candidates(
            repository,
            resolver,
        ),
    )
    exit_code = app.exec()
    logger.info("应用退出")
    return exit_code


def _load_database_config(store: EncryptedJsonStore) -> MySQLConfig:
    raw = store.load()
    if raw is None:
        dialog = DatabaseConfigDialog(store)
        if dialog.exec() != dialog.DialogCode.Accepted or dialog.saved_config is None:
            raise StartupCancelled()
        return dialog.saved_config
    return MySQLConfig(
        host=str(raw["host"]),
        port=int(raw["port"]),
        database=str(raw["database"]),
        user=str(raw["user"]),
        password=str(raw["password"]),
        charset=str(raw.get("charset", "utf8mb4")),
    )


def _ensure_central_schema(repository: MySQLCentralRepository) -> None:
    try:
        repository.schema_version()
    except SchemaNotInitializedError:
        repository.ensure_schema()


def _ensure_initial_admin(repository: MySQLCentralRepository) -> None:
    if repository.list_users():
        return
    dialog = InitialAdminDialog(repository)
    if dialog.exec() != dialog.DialogCode.Accepted:
        raise StartupCancelled()


def _cleanup_collaboration_candidates(
    repository: MySQLCentralRepository,
    resolver: SharedPathResolver,
) -> None:
    try:
        task_ids = [
            task.id
            for project in repository.list_projects()
            for task in repository.list_project_tasks(project.id)
        ]
        cleanup_expired_candidates(repository, resolver, task_ids)
    except Exception:
        return
