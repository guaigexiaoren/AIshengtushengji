import importlib.util
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None

if HAS_PYSIDE:
    from PIL import Image
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication,
        QLabel,
        QMessageBox,
        QPushButton,
    )

    from yixin.api import NanoBananaClient
    from yixin.collaboration.folders import current_date_folder
    from yixin.collaboration.reports import aggregate_report
    from yixin.collaboration.shared_paths import SharedPathResolver
    from yixin.collaboration.types import (
        ATTEMPT_QUEUED,
        ROLE_ADMIN,
        ROLE_OPERATIONS,
        AuthUser,
        GenerationAttempt,
        GenerationResult,
        PairingInput,
        ProjectAssetInput,
        ProjectRecord,
        ProjectTask,
    )
    from yixin.database import Database
    from yixin.library import LibraryService
    from yixin.paths import AppPaths
    from yixin.security import SecretStore
    from yixin.settings import AppSettings
    from yixin.storage import FileStore
    from yixin.ui.admin_page import AdminPage
    from yixin.ui.collaboration_login import LoginDialog
    from yixin.ui.dialogs import SettingsDialog
    from yixin.ui.main_window import MainWindow, SidebarNav
    from yixin.ui.submission_list_page import SubmissionListPage
    from yixin.ui.task_center_page import TaskCenterPage
    from yixin.ui.task_create_page import (
        CheckableHeaderView,
        PairPreviewCard,
        TaskCreatePage,
    )
    from yixin.ui.theme import APP_STYLESHEET
    from yixin.worker import TaskWorker


class FakeCentralRepository:
    def __init__(self) -> None:
        self.created_drafts = []
        self.generation_requests = []

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

    def current_model_price(self, _model):
        return Decimal("1.25")

    def report(self, _filter):
        return aggregate_report([])

    def test_connection(self):
        return {"db": "test", "version": "8.4"}

    def claim_and_queue_generation(self, task_id, actor, **kwargs):
        self.generation_requests.append((task_id, actor, kwargs))
        return GenerationAttempt(
            id=7,
            task_id=task_id,
            attempt_no=1,
            operator_id=actor.id,
            model=kwargs["model"],
            prompt_text=kwargs["prompt"],
            aspect_ratio=kwargs["aspect_ratio"],
            resolution=kwargs["resolution"],
            n=kwargs["n"],
            status=ATTEMPT_QUEUED,
            source_revision=1,
            unit_price=kwargs["unit_price"],
            generated_image_count=0,
            operator_name=actor.display_name,
            created_at=datetime.now(),
        )


def make_project(
    status: str = "in_progress",
) -> ProjectRecord:
    now = datetime.now()
    return ProjectRecord(
        id=1,
        project_code="春季项目-123-001",
        name="123",
        deadline=now + timedelta(days=1),
        priority="normal",
        brand_platform="",
        category="",
        note="重点检查领口",
        status=status,
        owner_id=3,
        owner_name="运营小林",
        project_relative_root="春季项目/1.2/123",
        pairing_mode="all",
        created_at=now,
        updated_at=now,
        task_count=1,
        product_code="123",
        source_date=date(2026, 1, 2),
    )


def make_task(status: str) -> ProjectTask:
    now = datetime.now()
    garments = (
        ProjectAssetInput(
            "garment",
            "衣服图/衣服1.png",
            "衣服1.png",
            "a" * 64,
            "image/png",
            20,
            20,
        ),
        ProjectAssetInput(
            "garment",
            "衣服图/衣服2.png",
            "衣服2.png",
            "b" * 64,
            "image/png",
            20,
            20,
        ),
    )
    return ProjectTask(
        id=11,
        task_code="春季项目-123-001-001",
        project_id=1,
        project_code="春季项目-123-001",
        project_name="123",
        project_relative_root="春季项目/1.2/123",
        garment_asset_id=2,
        garment_path=garments[0].relative_path,
        garment_name=garments[0].original_name,
        garment_hash=garments[0].content_hash,
        model_asset_id=1,
        model_path="模特图/模特1.png",
        model_name="模特1.png",
        model_hash="c" * 64,
        status=status,
        assignee_id=4,
        assignee_name="AI制作Amy",
        prompt_text="保留模特姿势，替换为图一服装",
        model="gemini-3.1-flash-image",
        aspect_ratio="3:4",
        resolution="2K",
        n=1,
        final_quantity=1,
        owner_id=3,
        created_at=now,
        source_date=date(2026, 1, 2),
        product_code="123",
        garment_assets=garments,
    )


def write_task_images(root: Path) -> None:
    product = root / "春季项目" / "1.2" / "123"
    (product / "衣服图").mkdir(parents=True)
    (product / "模特图").mkdir(parents=True)
    Image.new("RGB", (20, 20), "red").save(product / "衣服图" / "衣服1.png")
    Image.new("RGB", (20, 20), "green").save(product / "衣服图" / "衣服2.png")
    Image.new("RGB", (20, 20), "blue").save(product / "模特图" / "模特1.png")


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
            AppSettings(Database(self.root / "preview.db")),
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
        nav = SidebarNav(
            labels,
            group_before={0: "AI图提交"},
            submenu_indices={0, 1, 2, 3},
        )
        nav.setStyleSheet(APP_STYLESHEET)
        nav.resize(210, 650)
        nav.show()
        self.app.processEvents()
        self.assertEqual(nav.count(), len(labels))
        self.assertTrue(all(not button.isHidden() for button in nav._buttons))
        self.assertTrue(
            all(
                nav._buttons[index].property("submenu") in (True, "true")
                for index in range(4)
            )
        )
        self.assertFalse(nav._buttons[4].property("submenu"))
        group_label = nav.findChild(QLabel, "NavGroup")
        self.assertIsNotNone(group_label)
        self.assertEqual(
            group_label.font().pixelSize(),
            nav._buttons[4].font().pixelSize(),
        )
        nav.close()
        nav.deleteLater()

    def test_new_task_default_directory_and_custom_date_detection(self) -> None:
        page = TaskCreatePage(
            self.repository,
            self.resolver,
            self.actor,
        )
        expected_suffix = f"{date.today().month}.{date.today().day}"
        self.assertTrue(page.account_path.text().endswith(expected_suffix))
        custom = self.root / self.actor.display_name / "2026.12.26"
        custom.mkdir(parents=True)
        page.account_path.setText(str(custom))
        page.refresh()
        self.assertEqual(
            page.account_directory.source_date,
            date(2026, 12, 26),
        )
        fallback = self.root / self.actor.display_name / "临时目录"
        fallback.mkdir(parents=True)
        page.account_path.setText(str(fallback))
        page.refresh()
        self.assertEqual(page.account_directory.source_date, date.today())
        self.assertFalse(page.account_directory.date_recognized)
        page.deleteLater()

    def test_pair_preview_card_shows_model_and_all_garments(self) -> None:
        product = self.root / "配对"
        (product / "衣服图").mkdir(parents=True)
        (product / "模特图").mkdir(parents=True)
        garments = []
        for index in range(7):
            path = product / "衣服图" / f"衣服{index + 1}.png"
            Image.new("RGB", (20, 20), "red").save(path)
            garments.append(path)
        model = product / "模特图" / "模特1.png"
        Image.new("RGB", (20, 20), "blue").save(model)
        card = PairPreviewCard(model, garments)
        card.show()
        self.app.processEvents()
        labels = [label.text() for label in card.findChildren(QLabel)]
        self.assertNotIn("模特1.png", labels)
        self.assertIn("衣服 7 张 · 1 个子任务", labels)
        self.assertEqual(len(card.garment_thumbnails), 7)
        card.close()
        card.deleteLater()

    def test_product_header_selects_only_eligible_rows(self) -> None:
        date_folder = current_date_folder(
            self.root / self.actor.display_name
        )
        for code in ("123", "456", "重复货号"):
            product = date_folder.path / code
            (product / "衣服图").mkdir(parents=True)
            (product / "模特图").mkdir(parents=True)
            Image.new("RGB", (20, 20), "red").save(
                product / "衣服图" / "衣服1.png"
            )
            Image.new("RGB", (20, 20), "blue").save(
                product / "模特图" / "模特1.png"
            )
        invalid = date_folder.path / "缺少模特"
        (invalid / "衣服图").mkdir(parents=True)
        (invalid / "模特图").mkdir()
        Image.new("RGB", (20, 20), "red").save(
            invalid / "衣服图" / "衣服1.png"
        )
        self.repository.find_tasks_by_product = (
            lambda _actor, _date, code: [object()]
            if code == "重复货号"
            else []
        )
        page = TaskCreatePage(
            self.repository,
            self.resolver,
            self.actor,
        )
        header = page.product_table.horizontalHeader()
        self.assertIsInstance(header, CheckableHeaderView)
        self.assertFalse(hasattr(page, "select_new_button"))
        self.assertEqual(
            page.product_table.horizontalHeaderItem(0).text(),
            "选择",
        )
        states = {
            page.product_table.item(row, 1).text():
            page.product_table.item(row, 0)
            for row in range(page.product_table.rowCount())
        }
        self.assertTrue(
            states["123"].flags()
            & Qt.ItemFlag.ItemIsUserCheckable
        )
        self.assertFalse(
            states["重复货号"].flags()
            & Qt.ItemFlag.ItemIsUserCheckable
        )
        self.assertFalse(
            states["缺少模特"].flags()
            & Qt.ItemFlag.ItemIsUserCheckable
        )
        self.assertEqual(header.check_state(), Qt.CheckState.Checked)
        states["123"].setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(
            header.check_state(),
            Qt.CheckState.PartiallyChecked,
        )
        page._set_all_eligible(Qt.CheckState.Unchecked)
        self.assertEqual(
            header.check_state(),
            Qt.CheckState.Unchecked,
        )
        page._set_all_eligible(Qt.CheckState.Checked)
        self.assertEqual(header.check_state(), Qt.CheckState.Checked)
        QTest.mouseClick(
            header.viewport(),
            Qt.MouseButton.LeftButton,
            pos=QPoint(12, header.height() // 2),
        )
        self.assertEqual(
            header.check_state(),
            Qt.CheckState.Unchecked,
        )
        self.assertEqual(page._selected_codes(), [])
        page.deleteLater()

    def test_pair_preview_uses_four_columns_and_manual_selection(self) -> None:
        date_folder = current_date_folder(
            self.root / self.actor.display_name
        )
        product = date_folder.path / "123"
        (product / "衣服图").mkdir(parents=True)
        (product / "模特图").mkdir(parents=True)
        garment = product / "衣服图" / "衣服1.png"
        Image.new("RGB", (20, 20), "red").save(garment)
        models = []
        for index in range(5):
            path = product / "模特图" / f"模特{index + 1}.png"
            Image.new("RGB", (20, 20), "blue").save(path)
            models.append(path)
        page = TaskCreatePage(
            self.repository,
            self.resolver,
            self.actor,
        )
        self.assertEqual(
            page.pair_preview.minimumHeight(),
            page.pair_preview.maximumHeight(),
        )
        self.assertEqual(len(page.pair_cards), 5)
        positions = [
            page.pair_preview_layout.getItemPosition(index)
            for index in range(page.pair_preview_layout.count())
        ]
        self.assertEqual(
            positions[:4],
            [
                (0, 0, 1, 1),
                (0, 1, 1, 1),
                (0, 2, 1, 1),
                (0, 3, 1, 1),
            ],
        )
        self.assertEqual(positions[4][:2], (1, 0))
        page.mode_combo.setCurrentIndex(
            page.mode_combo.findData("manual")
        )
        state = page.current_state
        state.manual_pairs.clear()
        state.manual_pairs.extend(
            PairingInput(0, index)
            for index in range(len(models))
        )
        page._refresh_pair_preview()
        self.assertEqual(len(page.pair_cards), 5)
        page._select_manual_pair(0)
        self.assertEqual(page.selected_pair_model_index, 0)
        self.assertTrue(page.pair_cards[0]._selected)
        page._remove_manual_pair()
        self.assertEqual(len(state.manual_pairs), 4)
        self.assertEqual(len(page.pair_cards), 4)
        page.deleteLater()

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

    def test_submission_pages_have_requested_columns_and_actions(self) -> None:
        write_task_images(self.root)
        project = make_project()
        submitted_task = make_task("claimed")
        pending_task = make_task("awaiting_acceptance")
        completed_task = make_task("approved")
        product_root = self.root / pending_task.project_relative_root
        result_path = (
            product_root
            / "AI成品"
            / pending_task.project_code
            / pending_task.task_code
            / "attempt_001"
            / "result_01.png"
        )
        result_path.parent.mkdir(parents=True)
        Image.new("RGB", (40, 50), "purple").save(result_path)
        result = GenerationResult(
            id=19,
            attempt_id=2,
            result_index=0,
            relative_path=result_path.relative_to(product_root).as_posix(),
            thumbnail_relative_path=result_path.relative_to(
                product_root
            ).as_posix(),
            content_hash="f" * 64,
            mime="image/png",
            width=40,
            height=50,
            is_final=True,
        )
        self.repository.list_projects = lambda **_: [project]
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            submitted_task
        ]
        self.repository.list_results = lambda _task_id: [result]
        submitted = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "submitted",
        )
        submitted.show()
        self.app.processEvents()
        self.assertNotIn(
            "模特图",
            [
                submitted.task_table.horizontalHeaderItem(index).text()
                for index in range(submitted.task_table.columnCount())
            ],
        )
        edit = submitted.task_table.cellWidget(0, 6)
        self.assertIsInstance(edit, QPushButton)
        self.assertEqual(edit.text(), "修改素材与配对")
        self.assertTrue(submitted.urgent_button.isVisible())
        self.assertFalse(submitted.preview_tabs.isTabVisible(1))
        self.assertEqual(submitted.reference_strip.count(), 3)
        self.assertEqual(submitted.reference_strip.currentRow(), 2)
        self.assertEqual(submitted.reference_strip.item(2).text(), "模特")
        self.assertTrue(
            all(
                not submitted.reference_strip.item(index).icon().isNull()
                for index in range(submitted.reference_strip.count())
            )
        )
        self.assertEqual(
            submitted.reference_preview.current_path,
            (product_root / "模特图" / "模特1.png").resolve(),
        )
        submitted.reference_strip.setCurrentRow(0)
        self.app.processEvents()
        self.assertEqual(
            submitted.reference_preview.current_path,
            (product_root / "衣服图" / "衣服1.png").resolve(),
        )
        self.assertEqual(
            submitted.info_line.text(),
            "123 · 2026-01-02 · 运营小林 · 重点检查领口",
        )
        submitted.close()
        submitted.deleteLater()

        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            pending_task
        ]
        pending = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "pending",
        )
        pending.show()
        self.app.processEvents()
        self.assertFalse(pending.urgent_button.isVisible())
        self.assertTrue(pending.view_results.isVisible())
        self.assertTrue(pending.return_button.isVisible())
        self.assertTrue(pending.approve_button.isVisible())
        self.assertIsNone(pending.task_table.cellWidget(0, 6))
        self.assertTrue(pending.preview_tabs.isTabVisible(1))
        self.assertEqual(pending.result_gallery.list_widget.count(), 1)
        self.assertEqual(
            pending.result_gallery.preview.current_path,
            result_path.resolve(),
        )
        pending.close()
        pending.deleteLater()

        completed_project = make_project("completed")
        self.repository.list_projects = lambda **_: [completed_project]
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            completed_task
        ]
        completed = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "completed",
        )
        completed.show()
        self.app.processEvents()
        self.assertFalse(completed.urgent_button.isVisible())
        self.assertFalse(completed.view_results.isVisible())
        self.assertFalse(completed.return_button.isVisible())
        self.assertFalse(completed.approve_button.isVisible())
        completed.close()
        completed.deleteLater()

    def test_task_center_layout_retry_and_atomic_queue_action(self) -> None:
        class KeyStore:
            @staticmethod
            def load():
                return "test-key"

        center = TaskCenterPage(
            self.repository,
            self.resolver,
            self.actor,
            NanoBananaClient(lambda: "test-key"),
            KeyStore(),
            FileStore(AppPaths(self.root / "data")),
            AppSettings(Database(self.root / "preview.db")),
        )
        self.assertFalse(hasattr(center, "claim_button"))
        self.assertEqual(
            [
                center.retry_button.text(),
                center.save_button.text(),
                center.upload_button.text(),
                center.compare_button.text(),
                center.submit_button.text(),
            ],
            [
                "重试任务",
                "保存图片",
                "上传修后图",
                "查看/对比成品",
                "提交验收",
            ],
        )
        task = make_task("available")
        center.current_task = task
        center.prompt_edit.setPlainText("临时修改")
        with patch.object(center.prompt_edit, "setFocus") as set_focus:
            center._retry_task()
        self.assertEqual(center.prompt_edit.toPlainText(), task.prompt_text)
        set_focus.assert_called_once_with()
        center.prompt_edit.setPlainText("新的提示词")
        center.refresh = lambda: None
        center._enqueue_generation()
        self.assertEqual(len(self.repository.generation_requests), 1)
        self.assertEqual(
            self.repository.generation_requests[0][0],
            task.id,
        )
        center.deleteLater()

    def test_task_center_uses_source_strip_and_current_results(self) -> None:
        write_task_images(self.root)
        task = make_task("working")
        product_root = self.root / task.project_relative_root
        result_path = (
            product_root
            / "AI成品"
            / task.project_code
            / task.task_code
            / "attempt_001"
            / "result_01.png"
        )
        result_path.parent.mkdir(parents=True)
        Image.new("RGB", (50, 70), "orange").save(result_path)
        result = GenerationResult(
            id=21,
            attempt_id=4,
            result_index=0,
            relative_path=result_path.relative_to(product_root).as_posix(),
            thumbnail_relative_path=result_path.relative_to(
                product_root
            ).as_posix(),
            content_hash="a" * 64,
            mime="image/png",
            width=50,
            height=70,
            is_final=False,
        )
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            task
        ]
        self.repository.list_results = lambda _task_id: [result]
        center = TaskCenterPage(
            self.repository,
            self.resolver,
            self.actor,
            NanoBananaClient(lambda: "test-key"),
            type(
                "KeyStore",
                (),
                {"load": staticmethod(lambda: "test-key")},
            )(),
            FileStore(AppPaths(self.root / "task-center-data")),
            AppSettings(Database(self.root / "task-center.db")),
        )
        self.assertNotIn("\n", center.task_detail.text())
        self.assertIn("衣服 2 张", center.task_detail.text())
        self.assertEqual(center.source_strip.count(), 3)
        self.assertEqual(center.source_strip.currentRow(), 2)
        self.assertEqual(center.source_strip.item(0).text(), "衣服 1")
        self.assertEqual(center.source_strip.item(2).text(), "模特")
        self.assertTrue(
            all(
                not center.source_strip.item(index).icon().isNull()
                for index in range(center.source_strip.count())
            )
        )
        self.assertEqual(
            center.source_preview.current_path,
            (product_root / "模特图" / "模特1.png").resolve(),
        )
        center.source_strip.setCurrentRow(0)
        self.app.processEvents()
        self.assertEqual(
            center.source_preview.current_path,
            (product_root / "衣服图" / "衣服1.png").resolve(),
        )
        self.assertEqual(center.result_title.text(), "当前任务生成结果")
        self.assertEqual(center.gallery.list_widget.count(), 1)
        self.assertEqual(
            center.gallery.preview.current_path,
            result_path.resolve(),
        )
        center.deleteLater()

    def test_task_center_saves_only_current_original_result(self) -> None:
        write_task_images(self.root)
        task = make_task("working")
        source = (
            self.root
            / task.project_relative_root
            / "AI成品"
            / "春季项目-123-001"
            / task.task_code
            / "attempt_001"
            / "result_01.png"
        )
        source.parent.mkdir(parents=True)
        Image.new("RGB", (64, 80), "purple").save(source)
        result = GenerationResult(
            id=9,
            attempt_id=3,
            result_index=1,
            relative_path=source.relative_to(
                self.root / task.project_relative_root
            ).as_posix(),
            thumbnail_relative_path=source.relative_to(
                self.root / task.project_relative_root
            ).as_posix(),
            content_hash="d" * 64,
            mime="image/png",
            width=64,
            height=80,
            is_final=True,
        )
        database = Database(self.root / "save-settings.db")
        database.initialize()
        settings = AppSettings(database)
        output = self.root / "本地保存"
        output.mkdir()
        settings.set_local_save_mode("fixed")
        settings.set_fixed_folder(output)
        center = TaskCenterPage(
            self.repository,
            self.resolver,
            self.actor,
            NanoBananaClient(lambda: "test-key"),
            type(
                "KeyStore",
                (),
                {"load": staticmethod(lambda: "test-key")},
            )(),
            FileStore(AppPaths(self.root / "save-data")),
            settings,
        )
        center.current_task = task
        center.gallery.set_task(task, [result])
        with patch(
            "yixin.ui.task_center_page.show_saved_toast"
        ) as toast:
            center._save_result()
        saved = list(output.glob("*.png"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), source.read_bytes())
        toast.assert_called_once()
        center.deleteLater()


if __name__ == "__main__":
    unittest.main()
