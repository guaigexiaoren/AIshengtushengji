import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None

if HAS_PYSIDE:
    from PIL import Image
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

    from yixin.api import NanoBananaClient
    from yixin.collaboration.folders import current_date_folder
    from yixin.collaboration.reports import aggregate_report
    from yixin.collaboration.shared_paths import SharedPathResolver
    from yixin.collaboration.types import (
        ROLE_ADMIN,
        ROLE_OPERATIONS,
        AuthUser,
    )
    from yixin.database import Database
    from yixin.library import LibraryService
    from yixin.paths import AppPaths
    from yixin.security import SecretStore
    from yixin.storage import FileStore
    from yixin.ui.admin_page import AdminPage
    from yixin.ui.collaboration_login import LoginDialog
    from yixin.ui.dialogs import SettingsDialog
    from yixin.ui.main_window import MainWindow, SidebarNav
    from yixin.ui.submission_list_page import SubmissionListPage
    from yixin.ui.task_center_page import TaskCenterPage
    from yixin.ui.task_create_page import TaskCreatePage
    from yixin.worker import TaskWorker


class FakeCentralRepository:
    def __init__(self) -> None:
        self.created_drafts = []

    def create_project(self, _actor, draft):
        self.created_drafts.append(draft)
        return 99

    def find_tasks_by_product(self, *_args):
        return []

    def update_project(self, *_args):
        pass

    def list_projects(self, **_kwargs):
        return []

    def list_project_tasks(self, _project_id):
        return []

    def list_board_tasks(self, _actor, **_kwargs):
        return []

    def list_attempts(self, _task_id):
        return []

    def list_results(self, _task_id):
        return []

    def list_users(self, role=None):
        return []

    def api_key_statuses(self):
        return {}

    def list_model_prices(self):
        return {}

    def list_templates(self, _actor):
        return []

    def get_shared_setting(self, _key):
        return None

    def get_account_policy(self, _user_id):
        return {
            "urgent_quota_per_day": 3,
            "max_generation_concurrency": 2,
        }

    def load_actor_api_key(self, _actor):
        return None

    def report(self, _filter):
        return aggregate_report([])

    def test_connection(self):
        return {"db": "test", "version": "8.4"}


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
        self.actor = AuthUser(1, "admin", "春季项目", ROLE_ADMIN)
        self.resolver = SharedPathResolver(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_new_pages_build_with_empty_database(self) -> None:
        create = TaskCreatePage(
            self.repository,
            self.resolver,
            self.actor,
        )
        submitted = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "submitted",
        )
        pending = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "pending",
        )
        completed = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "completed",
        )
        center = TaskCenterPage(
            self.repository,
            self.resolver,
            self.actor,
            NanoBananaClient(lambda: None),
            SecretStore(self.root / "unused.bin"),
            FileStore(AppPaths(self.root / "data")),
        )
        admin = AdminPage(self.repository, self.actor)
        self.assertEqual(create.product_table.rowCount(), 0)
        self.assertEqual(submitted.project_table.rowCount(), 0)
        self.assertEqual(pending.project_table.rowCount(), 0)
        self.assertEqual(completed.project_table.rowCount(), 0)
        self.assertEqual(center.task_table.rowCount(), 0)
        self.assertEqual(admin.user_table.rowCount(), 0)
        for page in (create, submitted, pending, completed, center, admin):
            page.deleteLater()

    def test_login_has_no_database_settings(self) -> None:
        login = LoginDialog(self.repository)
        texts = [button.text() for button in login.findChildren(QPushButton)]
        self.assertFalse(any("数据库设置" in text for text in texts))
        login.deleteLater()

    def test_ai_settings_have_no_api_key_input(self) -> None:
        paths = AppPaths(self.root / "settings-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        dialog = SettingsDialog(database)
        self.assertFalse(hasattr(dialog, "key_edit"))
        self.assertFalse(hasattr(dialog, "clear_button"))
        dialog.deleteLater()

    def test_sidebar_shows_all_new_menu_items(self) -> None:
        labels = [
            "新建任务",
            "已提交",
            "待审核",
            "已完成",
            "任务中心",
            "普通创作",
            "批量换装",
            "历史记录",
            "作品库",
            "管理控制台",
        ]
        nav = SidebarNav(labels, group_before={0: "AI图提交"})
        nav.resize(210, 650)
        nav.show()
        self.app.processEvents()
        self.assertEqual(nav.count(), len(labels))
        self.assertTrue(all(not button.isHidden() for button in nav._buttons))
        nav.close()
        nav.deleteLater()

    def test_admin_user_table_shows_masked_api_key_and_policies(self) -> None:
        ai_user = AuthUser(
            8,
            "ai_amy",
            "AI制作Amy",
            "ai",
            urgent_quota_per_day=4,
            max_generation_concurrency=3,
        )
        self.repository.list_users = lambda role=None: [ai_user]
        self.repository.api_key_statuses = lambda: {
            8: {
                "key_suffix": "9X2A",
                "updated_at": datetime.now(),
            }
        }
        page = AdminPage(self.repository, self.actor)
        self.assertEqual(page.user_table.rowCount(), 1)
        self.assertIn("9X2A", page.user_table.item(0, 5).text())
        self.assertEqual(page.user_table.item(0, 7).text(), "4")
        self.assertEqual(page.user_table.item(0, 8).text(), "3")
        page.deleteLater()

    def test_auto_scan_builds_one_subtask_per_model(self) -> None:
        date_folder = current_date_folder(
            self.root / self.actor.display_name
        )
        product = date_folder.path / "123"
        garment = product / "衣服图"
        model = product / "模特图"
        garment.mkdir(parents=True)
        model.mkdir()
        for index in range(3):
            Image.new("RGB", (20, 20), "red").save(
                garment / f"衣服{index + 1}.png"
            )
        for index in range(2):
            Image.new("RGB", (20, 20), "blue").save(
                model / f"模特{index + 1}.png"
            )
        page = TaskCreatePage(
            self.repository,
            self.resolver,
            self.actor,
        )
        self.assertEqual(page.product_table.rowCount(), 1)
        page.product_table.selectRow(0)
        page.product_table.item(0, 0).setCheckState(
            page.product_table.item(0, 0).checkState()
        )
        with (
            patch(
                "yixin.ui.task_create_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch("yixin.ui.task_create_page.QMessageBox.information"),
        ):
            page._create_selected()
        self.assertEqual(len(self.repository.created_drafts), 1)
        draft = self.repository.created_drafts[0]
        self.assertEqual(draft.product_code, "123")
        self.assertEqual(draft.project_code, "")
        self.assertEqual(len(draft.pairs), 6)
        page.deleteLater()

    def test_operations_main_window_has_new_task_navigation(self) -> None:
        paths = AppPaths(self.root / "data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
        actor = AuthUser(3, "operator", "运营小林", ROLE_OPERATIONS)
        client = NanoBananaClient(lambda: None)
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
        labels = [
            window.nav_list.item(index).text()
            for index in range(window.nav_list.count())
        ]
        self.assertEqual(
            labels,
            ["新建任务", "已提交", "待审核", "已完成", "任务中心"],
        )
        window.close()
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
