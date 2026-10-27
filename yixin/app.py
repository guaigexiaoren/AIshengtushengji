from __future__ import annotations

import ctypes
import os
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, Qt, QTimer, QTranslator
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .api import NanoBananaClient
from .collaboration.cleanup import cleanup_expired_candidates
from .collaboration.api_keys import DatabaseApiKeyStore
from .collaboration.repository import (
    MySQLCentralRepository,
    MySQLConfig,
    SCHEMA_VERSION,
    SchemaNotInitializedError,
)
from .collaboration.shared_paths import SharedPathResolver
from .collaboration.upgrade import prepare_legacy_data
from .constants import APP_NAME, APP_ORGANIZATION, TEMP_RETENTION_DAYS
from .database import Database
from .library import LibraryService
from .logging_utils import register_sensitive_value, setup_logging
from .paths import AppPaths, resource_path
from .private_config import DATABASE, SHARED_ROOT
from .storage import FileStore
from .ui.main_window import MainWindow
from .ui.collaboration_login import (
    InitialAdminDialog,
    LoginDialog,
)
from .ui.theme import APP_STYLESHEET
from .ui.localization import install_chinese_dialog_filter
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
    QApplication.setAttribute(
        Qt.ApplicationAttribute.AA_DontUseNativeDialogs,
        True,
    )
    app = QApplication.instance() or QApplication([])
    QLocale.setDefault(
        QLocale(
            QLocale.Language.Chinese,
            QLocale.Territory.China,
        )
    )
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
    install_chinese_dialog_filter(app)

    paths = AppPaths.default()
    try:
        paths.ensure()
        paths.api_key_file.unlink(missing_ok=True)
        (paths.config / "database.bin").unlink(missing_ok=True)
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
        database_config = MySQLConfig(**DATABASE)
        repository = MySQLCentralRepository(database_config)
        _ensure_central_schema(repository)
        _ensure_initial_admin(repository)
        login = LoginDialog(repository)
        if login.exec() != login.DialogCode.Accepted or login.user is None:
            return 0
        actor = login.user
        repository = login.repository
        actor = repository.load_user_access(actor)
        repository.recover_stale_generation_attempts()
        shared_root = repository.get_shared_setting("share_root")
        resolver = SharedPathResolver(Path(shared_root or SHARED_ROOT))
        secret_store = DatabaseApiKeyStore(repository, actor)
        try:
            register_sensitive_value(secret_store.load())
        except Exception:
            pass
        client = NanoBananaClient(lambda: secret_store.load())
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


def _ensure_central_schema(repository: MySQLCentralRepository) -> None:
    try:
        version = repository.schema_version()
    except SchemaNotInitializedError:
        version = 0
    if version < SCHEMA_VERSION:
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
