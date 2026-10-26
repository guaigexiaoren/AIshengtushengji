import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None

if HAS_PYSIDE:
    from PySide6.QtWidgets import QApplication
    from PIL import Image

    from yixin.api import NanoBananaClient
    from yixin.collaboration.reports import aggregate_report
    from yixin.collaboration.shared_paths import SharedPathResolver
    from yixin.collaboration.types import (
        ROLE_ADMIN,
        ROLE_AI,
        ROLE_OPERATIONS,
        AuthUser,
    )
    from yixin.database import Database
    from yixin.library import LibraryService
    from yixin.paths import AppPaths
    from yixin.security import SecretStore
    from yixin.storage import FileStore
    from yixin.ui.main_window import MainWindow, SidebarNav
    from yixin.ui.admin_page import AdminPage
    from yixin.ui.ai_projects_page import AiProjectsPage
    from yixin.ui.collaboration_login import LoginDialog
    from yixin.ui.operations_page import OperationsPage
    from yixin.ui.project_editor import ProjectEditorDialog
    from yixin.worker import TaskWorker


class FakeCentralRepository:
    def __init__(self) -> None:
        self.created_draft = None

    def create_project(self, _actor, draft):
        self.created_draft = draft
        return 99

    def list_projects(self):
        return []

    def list_project_tasks(self, _project_id):
        return []

    def list_board_tasks(self, _actor, **_kwargs):
        return []

    def list_users(self, role=None):
        return []

    def list_model_prices(self):
        return {}

    def list_templates(self, _actor):
        return []

    def get_shared_setting(self, _key):
        return None

    def report(self, _filter):
        return aggregate_report([])

    def test_connection(self):
        return {"db": "test", "version": "8.4"}


class FakeConfigStore:
    def load(self):
        return {
            "host": "127.0.0.1",
            "port": 3306,
            "database": "test",
            "user": "tester",
            "password": "secret",
        }

    def save(self, _value):
        pass


@unittest.skipUnless(HAS_PYSIDE, "PySide6 未安装")
class CollaborationPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = FakeCentralRepository()
        self.actor = AuthUser(1, "admin", "管理员", ROLE_ADMIN)
        self.resolver = SharedPathResolver(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_role_pages_build_with_empty_database(self) -> None:
        operations = OperationsPage(
            self.repository,
            self.resolver,
            self.actor,
        )
        projects = AiProjectsPage(
            self.repository,
            self.resolver,
            AuthUser(2, "ai", "制作", ROLE_AI),
            NanoBananaClient(lambda: None),
            SecretStore(self.root / "api.bin"),
            FileStore(AppPaths(self.root / "data")),
        )
        admin = AdminPage(self.repository, self.actor)
        self.assertEqual(operations.project_table.rowCount(), 0)
        self.assertEqual(projects.task_table.rowCount(), 0)
        self.assertEqual(admin.user_table.rowCount(), 0)
        for page in (operations, projects, admin):
            page.deleteLater()

    def test_project_editor_and_login_build(self) -> None:
        editor = ProjectEditorDialog(
            self.repository,
            self.resolver,
            self.actor,
        )
        login = LoginDialog(self.repository, FakeConfigStore())
        self.assertIn("新建运营项目", editor.windowTitle())
        self.assertEqual(login.username.text(), "")
        editor.deleteLater()
        login.deleteLater()

    def test_project_editor_builds_gm_pairs_and_saves_draft(self) -> None:
        project = self.root / "项目A"
        garment = project / "衣服图"
        model = project / "模特图"
        garment.mkdir(parents=True)
        model.mkdir()
        Image.new("RGB", (20, 20), "red").save(garment / "G001_衬衫.png")
        Image.new("RGB", (20, 20), "blue").save(model / "M001_模特.png")
        editor = ProjectEditorDialog(
            self.repository,
            self.resolver,
            self.actor,
        )
        editor.project_root = project.resolve()
        editor.folder_label.setText(str(project))
        editor._import_folder("garment", garment)
        editor._import_folder("model", model)
        editor.code_edit.setText("P001")
        editor.name_edit.setText("测试项目")
        editor.mode_combo.setCurrentIndex(
            editor.mode_combo.findData("filename")
        )
        with patch("yixin.ui.project_editor.QMessageBox.warning") as warning:
            editor._save()
        warning.assert_not_called()
        self.assertIsNotNone(self.repository.created_draft)
        self.assertEqual(len(self.repository.created_draft.pairs), 1)
        self.assertEqual(
            self.repository.created_draft.project_relative_root,
            "项目A",
        )
        editor.deleteLater()

    def test_operations_login_only_shows_operations_workspace(self) -> None:
        paths = AppPaths(self.root / "data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
        client = NanoBananaClient(lambda: None)
        actor = AuthUser(
            3,
            "operator",
            "运营A",
            ROLE_OPERATIONS,
        )
        window = MainWindow(
            paths=paths,
            database=database,
            file_store=file_store,
            library_service=LibraryService(database, file_store, paths),
            secret_store=SecretStore(paths.api_key_file),
            worker=TaskWorker(database, client, file_store),
            client=client,
            repository=self.repository,
            resolver=self.resolver,
            actor=actor,
        )
        self.assertEqual(window.nav_list.count(), 1)
        self.assertEqual(window.nav_list.item(0).text(), "运营项目")
        window.close()
        window.deleteLater()

    def test_sidebar_shows_every_menu_item_without_scroll_container(self) -> None:
        labels = ["运营项目", "项目制作", "普通创作", "批量换装", "历史记录", "作品库", "管理控制台"]
        nav = SidebarNav(labels)
        nav.resize(210, 420)
        nav.show()
        self.app.processEvents()
        self.assertEqual(nav.count(), len(labels))
        self.assertTrue(all(not button.isHidden() for button in nav._buttons))
        nav.setCurrentRow(3)
        self.assertEqual(nav.currentRow(), 3)
        self.assertTrue(nav._buttons[3].isChecked())
        nav.close()
        nav.deleteLater()


if __name__ == "__main__":
    unittest.main()
