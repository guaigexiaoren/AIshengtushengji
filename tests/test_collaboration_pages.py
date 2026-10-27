import importlib.util
import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None

if HAS_PYSIDE:
    from PIL import Image
    from PySide6.QtCore import QDate, QItemSelectionModel, QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QDialog,
        QHeaderView,
        QLabel,
        QMessageBox,
        QPushButton,
        QTableWidget,
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
    from yixin.constants import BUILTIN_PROMPT_TEMPLATES
    from yixin.database import Database
    from yixin.library import LibraryService
    from yixin.paths import AppPaths
    from yixin.security import SecretStore
    from yixin.settings import INPUT_MANUAL_UPLOAD, AppSettings
    from yixin.storage import FileStore
    from yixin.time_utils import utc_naive_to_local
    from yixin.ui.admin_page import AddUserDialog, AdminPage, EditUserDialog
    from yixin.ui.collaboration_login import LoginDialog
    from yixin.ui.dialogs import SettingsDialog
    from yixin.ui.main_window import MainWindow, SidebarNav
    from yixin.ui.submission_list_page import SubmissionListPage
    from yixin.ui.subtask_source_dialog import SubtaskSourceDialog
    from yixin.ui.project_history_page import ProjectHistoryPage
    from yixin.ui.history_page import HistoryTabsPage
    from yixin.ui.date_filter import OptionalDateEdit
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
        self.source_updates = []
        self.project_attempt_history = []
        self.attempt_results = {}
        self.tasks_by_id = {}
        self.user_updates = []
        self.skip_requests = []
        self.project_urgent_updates = []
        self.queued_attempts = []
        self.submission_requests = []

    def create_project(self, _actor, draft):
        self.created_drafts.append(draft)
        return 99

    def update_subtask_sources(self, task_id, actor, **kwargs):
        self.source_updates.append((task_id, actor, kwargs))

    def find_tasks_by_product(self, *_args):
        return []

    def update_project(self, *_args):
        pass

    def list_projects(self, **_kwargs):
        return []

    def list_accessible_projects(self, _actor):
        return (("internal", "春季项目"),)

    def list_shared_projects(self):
        return {
            "internal": ("春季项目", "夏季项目"),
            "external": ("客户A",),
        }

    def user_menu_permissions(self, _user_id):
        return (
            "new_task",
            "submitted",
            "pending",
            "completed",
            "task_center",
        )

    def user_project_access(self, _user_id):
        return (("internal", "春季项目"),)

    def remaining_urgent_quota(self, _actor, _day):
        return 3

    def set_project_urgent(self, project_id, actor, urgent):
        self.project_urgent_updates.append((project_id, actor, urgent))

    def list_project_tasks(self, _project_id):
        return []

    def list_board_tasks(self, _actor, **_kwargs):
        return []

    def list_attempts(self, _task_id):
        return []

    def list_queued_attempts(self, _operator_id):
        return list(self.queued_attempts)

    def list_results(self, _task_id):
        return []

    def get_task(self, task_id):
        return self.tasks_by_id.get(task_id)

    def list_project_attempt_history(self, _actor, **_kwargs):
        return list(self.project_attempt_history)

    def list_attempt_results(self, attempt_id):
        return list(self.attempt_results.get(attempt_id, []))

    def list_users(self, role=None):
        return []

    def update_user(self, actor, user_id, **kwargs):
        self.user_updates.append((actor, user_id, kwargs))

    def skip_remaining_subtasks(self, project_id, actor, *, reason):
        self.skip_requests.append((project_id, actor, reason))
        return 3

    def submit_for_acceptance(self, task_id, actor, **kwargs):
        self.submission_requests.append((task_id, actor, kwargs))

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
        project_relative_root="内部项目/春季项目/1.2/123",
        pairing_mode="all",
        created_at=now,
        updated_at=now,
        task_count=1,
        product_code="123",
        source_date=date(2026, 1, 2),
        project_scope="internal",
        project_name="春季项目",
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
        project_relative_root="内部项目/春季项目/1.2/123",
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
        project_scope="internal",
        access_project_name="春季项目",
        project_is_urgent=False,
    )


def write_task_images(root: Path) -> None:
    product = root / "内部项目" / "春季项目" / "1.2" / "123"
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

    def test_login_wrong_password_enter_keeps_dialog_open(self) -> None:
        class Repository:
            def authenticate_user(self, _username, _password):
                raise PermissionError("账号或密码不正确")

        login = LoginDialog(Repository())
        login.username.setText("operator")
        login.password.setText("wrong-password")
        login.show()
        self.app.processEvents()
        login.password.setFocus()
        with patch(
            "yixin.ui.collaboration_login.QMessageBox.warning"
        ) as warning:
            QTest.keyClick(login.password, Qt.Key.Key_Return)
            self.app.processEvents()
        warning.assert_called_once()
        self.assertTrue(login.isVisible())
        self.assertEqual(login.result(), 0)
        self.assertEqual(login.password.text(), "")
        login.close()
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
        expected_root = (
            self.root / "内部项目" / "春季项目" / expected_suffix
        ).resolve()
        self.assertEqual(
            Path(page.account_path.text()).resolve(),
            expected_root,
        )
        self.assertTrue(page.account_path.text().endswith(expected_suffix))
        project_root = self.root / "内部项目" / "春季项目"
        custom = project_root / "2026.12.26"
        custom.mkdir(parents=True)
        page.account_path.setText(str(custom))
        page.refresh()
        self.assertEqual(
            page.account_directory.source_date,
            date(2026, 12, 26),
        )
        fallback = project_root / "临时目录"
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
        self.assertEqual(card.thumbnail_layout.count(), 8)
        card.close()
        card.deleteLater()

    def test_product_header_selects_only_eligible_rows(self) -> None:
        date_folder = current_date_folder(
            self.root / "内部项目" / "春季项目"
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
            "状态",
        )
        self.assertEqual(
            page.product_table.horizontalHeaderItem(1).text(),
            "",
        )
        self.assertFalse(page.product_table.verticalHeader().isVisible())
        self.assertEqual(page.product_table.columnWidth(1), 42)
        states = {
            page.product_table.item(row, 2).text():
            page.product_table.item(row, 1)
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
            pos=QPoint(
                header.sectionViewportPosition(1) + 12,
                header.height() // 2,
            ),
        )
        self.assertEqual(
            header.check_state(),
            Qt.CheckState.Unchecked,
        )
        self.assertEqual(page._selected_codes(), [])
        page.deleteLater()

    def test_pair_preview_uses_four_columns_and_manual_selection(self) -> None:
        date_folder = current_date_folder(
            self.root / "内部项目" / "春季项目"
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

    def test_task_create_drag_folder_and_main_task_quota(self) -> None:
        date_folder = current_date_folder(
            self.root / "内部项目" / "春季项目"
        )
        product = date_folder.path / "123"
        (product / "衣服图").mkdir(parents=True)
        (product / "模特图").mkdir(parents=True)
        Image.new("RGB", (20, 20), "red").save(
            product / "衣服图" / "衣服1.png"
        )
        Image.new("RGB", (20, 20), "blue").save(
            product / "模特图" / "模特1.png"
        )
        incoming = self.root / "拖入素材"
        incoming.mkdir()
        Image.new("RGB", (20, 20), "green").save(
            incoming / "衣服2.png"
        )
        page = TaskCreatePage(
            self.repository,
            self.resolver,
            self.actor,
        )
        page.product_table.selectRow(0)
        page._handle_image_drop("garment", [incoming])
        self.assertEqual(len(page.current_state.garments), 2)
        self.assertTrue(
            (product / "衣服图" / "衣服2.png").is_file()
        )
        page.urgent_checkbox.setChecked(True)
        self.repository.remaining_urgent_quota = (
            lambda _actor, _day: 0
        )
        with patch(
            "yixin.ui.task_create_page.QMessageBox.warning"
        ) as warning:
            page._create_selected()
        warning.assert_called_once()
        self.assertEqual(self.repository.created_drafts, [])
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

    def test_admin_can_edit_account_fields_and_permissions(self) -> None:
        ai_user = AuthUser(
            8,
            "ai_amy",
            "AI制作Amy",
            "ai",
            urgent_quota_per_day=4,
            max_generation_concurrency=3,
        )
        self.repository.list_users = lambda role=None: [ai_user]
        page = AdminPage(self.repository, self.actor)
        page.user_table.selectRow(0)
        dialog = EditUserDialog(
            ai_user,
            self.repository.user_menu_permissions(ai_user.id),
            self.repository.user_project_access(ai_user.id),
            self.repository.list_shared_projects(),
        )
        dialog.username.setText("ai_amy_new")
        dialog.display_name.setText("AI制作Amy新")
        dialog.role.setCurrentIndex(dialog.role.findData("admin"))
        dialog.active.setChecked(True)
        dialog.urgent_quota.setValue(6)
        dialog.max_concurrency.setValue(5)
        with (
            patch(
                "yixin.ui.admin_page.EditUserDialog",
                return_value=dialog,
            ),
            patch.object(
                dialog,
                "exec",
                return_value=QDialog.DialogCode.Accepted,
            ),
        ):
            page._edit_user()
        self.assertEqual(len(self.repository.user_updates), 1)
        update = self.repository.user_updates[0][2]
        self.assertEqual(update["username"], "ai_amy_new")
        self.assertEqual(update["display_name"], "AI制作Amy新")
        self.assertEqual(update["role"], "admin")
        self.assertEqual(update["urgent_quota_per_day"], 6)
        self.assertEqual(update["max_generation_concurrency"], 5)
        self.assertIn("menu_permissions", update)
        self.assertIn("project_access", update)
        page.deleteLater()

    def test_admin_exception_filter_only_returns_abnormal_tasks(self) -> None:
        page = AdminPage(self.repository, self.actor)
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=24)
        normal = replace(
            make_task("awaiting_acceptance"),
            created_at=now,
            updated_at=now,
        )
        returned = replace(
            make_task("returned"),
            created_at=now,
            updated_at=now,
        )
        stale_available = replace(
            make_task("available"),
            created_at=cutoff - timedelta(hours=1),
            updated_at=cutoff - timedelta(hours=1),
        )
        self.assertFalse(
            page._task_needs_admin_attention(normal, cutoff)
        )
        self.assertTrue(
            page._task_needs_admin_attention(returned, cutoff)
        )
        self.assertTrue(
            page._task_needs_admin_attention(stale_available, cutoff)
        )
        page.deleteLater()

    def test_account_dialog_enforces_role_menu_ceiling(self) -> None:
        operator = AuthUser(
            9,
            "operator_9",
            "运营九组",
            ROLE_OPERATIONS,
        )
        dialog = EditUserDialog(
            operator,
            (
                "new_task",
                "submitted",
                "pending",
                "completed",
            ),
            (("internal", "春季项目"),),
            self.repository.list_shared_projects(),
        )
        self.assertTrue(dialog.permissions.menu_checks["task_center"].isEnabled())
        self.assertFalse(
            dialog.permissions.menu_checks["task_center"].isChecked()
        )
        self.assertFalse(
            dialog.permissions.menu_checks["admin"].isEnabled()
        )
        self.assertTrue(
            dialog.permissions.project_checks[
                ("internal", "春季项目")
            ].isChecked()
        )
        dialog.deleteLater()
        add_dialog = AddUserDialog(self.repository.list_shared_projects())
        self.assertFalse(
            add_dialog.permissions.menu_checks[
                "task_center"
            ].isChecked()
        )
        self.assertFalse(
            add_dialog.permissions.menu_checks["admin"].isEnabled()
        )
        add_dialog.deleteLater()

    def test_auto_scan_builds_one_subtask_per_model(self) -> None:
        date_folder = current_date_folder(
            self.root / "内部项目" / "春季项目"
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
        page.product_table.item(0, 1).setCheckState(
            page.product_table.item(0, 1).checkState()
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

    def test_subtask_source_dialog_adds_files_for_both_roles(self) -> None:
        write_task_images(self.root)
        task = make_task("claimed")
        product_root = self.root / task.project_relative_root
        assets = (
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
                "model",
                "模特图/模特1.png",
                "模特1.png",
                "c" * 64,
                "image/png",
                20,
                20,
            ),
        )
        external = self.root / "外部素材"
        external.mkdir()
        new_garment = external / "新增衣服.png"
        new_model = external / "新增模特.png"
        Image.new("RGB", (30, 40), "yellow").save(new_garment)
        Image.new("RGB", (30, 40), "cyan").save(new_model)
        dialog = SubtaskSourceDialog(
            self.repository,
            self.resolver,
            self.actor,
            task,
            assets,
        )
        dialog._stage_sources("garment", [new_garment])
        dialog._stage_sources("model", [new_model])
        dialog.garment_list.clearSelection()
        dialog.model_list.clearSelection()
        dialog.garment_list.item(dialog.garment_list.count() - 1).setSelected(
            True
        )
        dialog.model_list.item(dialog.model_list.count() - 1).setSelected(
            True
        )
        dialog._save()
        self.assertEqual(len(self.repository.source_updates), 1)
        update = self.repository.source_updates[0][2]
        roles = {asset.role for asset in update["new_assets"]}
        self.assertEqual(roles, {"garment", "model"})
        self.assertTrue(
            (product_root / "衣服图" / "新增衣服.png").is_file()
        )
        self.assertTrue(
            (product_root / "模特图" / "新增模特.png").is_file()
        )
        dialog.deleteLater()

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
        self.assertFalse(
            any(
                "任务候选图保留" in label.text()
                for label in window.findChildren(QLabel)
            )
        )
        self.assertEqual(
            labels,
            ["新建任务", "已提交", "待审核", "已完成"],
        )
        with patch.object(window.new_task_page, "refresh") as refresh:
            window.show()
            self.app.processEvents()
            QTest.qWait(20)
            refresh.assert_called_once_with()
        window.close()
        window.deleteLater()

    def test_submission_pages_have_requested_columns_and_actions(self) -> None:
        picker = OptionalDateEdit("开始日期")
        self.assertEqual(picker.text(), "开始日期")
        self.assertIsNone(picker.python_date())
        picker.setDate(QDate.currentDate())
        self.assertEqual(picker.python_date(), date.today())
        picker.clear()
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
        self.assertTrue(submitted.project_table.isSortingEnabled())
        self.assertTrue(submitted.task_table.isSortingEnabled())
        self.assertEqual(
            submitted.project_table.horizontalHeader().sectionResizeMode(0),
            QHeaderView.ResizeMode.Interactive,
        )
        self.assertEqual(
            submitted.task_table.horizontalHeader().sectionResizeMode(0),
            QHeaderView.ResizeMode.Interactive,
        )
        self.assertIsNone(submitted.start_date_edit.python_date())
        self.assertEqual(submitted.start_date_edit.text(), "开始日期")
        self.assertEqual(
            submitted.task_table.horizontalHeaderItem(5).text(),
            "创建时间",
        )
        self.assertEqual(
            submitted.task_table.columnCount(),
            7,
        )
        self.assertIsNone(submitted.task_table.cellWidget(0, 6))
        with patch.object(submitted, "_edit_task") as edit_task:
            submitted._edit_task_from_item(submitted.task_table.item(0, 0))
        edit_task.assert_called_once_with(submitted_task)
        with patch.object(submitted, "refresh"):
            submitted._toggle_urgent()
        self.assertEqual(
            self.repository.project_urgent_updates[-1][0],
            project.id,
        )
        self.assertTrue(self.repository.project_urgent_updates[-1][2])
        self.assertTrue(submitted.urgent_button.isVisible())
        self.assertTrue(submitted.result_panel.isHidden())
        self.assertFalse(submitted.reference_panel.isHidden())
        self.assertFalse(submitted.review_actions.isVisible())
        self.assertEqual(
            submitted.reference_header.text(),
            "参考图（3）",
        )
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
        submitted.search_edit.setText("不存在的货号")
        self.app.processEvents()
        self.assertEqual(submitted.project_table.rowCount(), 0)
        submitted.search_edit.clear()
        self.app.processEvents()
        self.assertEqual(submitted.project_table.rowCount(), 1)
        task_date = utc_naive_to_local(submitted_task.created_at).date()
        task_qdate = QDate(
            task_date.year,
            task_date.month,
            task_date.day,
        )
        submitted.start_date_edit.setDate(task_qdate)
        submitted.end_date_edit.setDate(task_qdate)
        self.app.processEvents()
        self.assertEqual(submitted.project_table.rowCount(), 1)
        submitted._reset_filters()
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
        self.assertTrue(pending.view_results.isEnabled())
        self.assertTrue(pending.review_actions.isVisible())
        self.assertFalse(pending.result_panel.isHidden())
        self.assertFalse(pending.reference_panel.isHidden())
        self.assertEqual(
            [
                pending.view_results.text(),
                pending.return_button.text(),
                pending.approve_button.text(),
            ],
            ["与原模特图对比", "退回返工", "验收通过"],
        )
        self.assertEqual(pending.task_table.columnCount(), 7)
        self.assertEqual(
            pending.result_header.text(),
            "结果图（1）",
        )
        self.assertEqual(
            pending.reference_header.text(),
            "参考图（3）",
        )
        self.assertEqual(pending.result_gallery.list_widget.count(), 1)
        self.assertEqual(
            pending.result_gallery.preview.current_path,
            result_path.resolve(),
        )
        with patch(
            "yixin.ui.submission_list_page.CompareDialog"
        ) as compare_dialog:
            pending.view_results.click()
        compare_dialog.assert_called_once()
        compare_dialog.return_value.exec.assert_called_once_with()
        pending.close()
        pending.deleteLater()

        self.repository.list_results = lambda _task_id: []
        pending_empty = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "pending",
        )
        pending_empty.show()
        self.app.processEvents()
        self.assertFalse(pending_empty.view_results.isEnabled())
        self.assertTrue(pending_empty.return_button.isEnabled())
        self.assertTrue(pending_empty.approve_button.isEnabled())
        self.assertEqual(
            pending_empty.result_header.text(),
            "结果图（0）",
        )
        pending_empty.close()
        pending_empty.deleteLater()
        self.repository.list_results = lambda _task_id: [result]

        completed_project = make_project("in_progress")
        self.repository.list_projects = lambda **_: [completed_project]
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            completed_task,
            pending_task,
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
        self.assertFalse(completed.review_actions.isVisible())
        self.assertFalse(completed.result_panel.isHidden())
        self.assertFalse(completed.reference_panel.isHidden())
        self.assertEqual(completed.project_table.rowCount(), 1)
        self.assertEqual(completed.task_table.rowCount(), 1)
        self.assertEqual(
            completed.task_table.item(0, 3).text(),
            "已完成",
        )
        completed.close()
        completed.deleteLater()

    def test_task_center_layout_retry_and_atomic_queue_action(self) -> None:
        write_task_images(self.root)

        class KeyStore:
            @staticmethod
            def load():
                return "test-key"

        settings_database = Database(self.root / "preview.db")
        settings_database.initialize()
        center = TaskCenterPage(
            self.repository,
            self.resolver,
            self.actor,
            NanoBananaClient(lambda: "test-key"),
            KeyStore(),
            FileStore(AppPaths(self.root / "data")),
            AppSettings(settings_database),
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
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [task]
        center.refresh()
        self.assertEqual(center.current_task.id, task.id)
        self.assertTrue(center.upload_button.isEnabled())
        self.assertTrue(center._can_upload_manual(task))
        self.assertEqual(
            center.project_table.horizontalHeader().sectionResizeMode(0),
            QHeaderView.ResizeMode.Interactive,
        )
        ps_image = self.root / "修后图.png"
        Image.new("RGB", (20, 20), "white").save(ps_image)
        with (
            patch(
                "yixin.ui.task_center_page.QFileDialog.getOpenFileNames",
                return_value=([str(ps_image)], ""),
            ) as first_open_files,
            patch(
                "yixin.ui.task_center_page.upload_manual_candidates",
                return_value=1,
            ),
            patch(
                "yixin.ui.task_center_page.QMessageBox.information"
            ),
            patch.object(center, "refresh"),
        ):
            center._upload_manual()
        self.assertEqual(
            first_open_files.call_args.args[2],
            str(Path.home()),
        )
        upload_history = self.root / "上次修后图目录"
        upload_history.mkdir()
        center.app_settings.set_input_folder(
            INPUT_MANUAL_UPLOAD,
            upload_history,
        )
        with (
            patch(
                "yixin.ui.task_center_page.QFileDialog.getOpenFileNames",
                return_value=([str(ps_image)], ""),
            ) as open_files,
            patch(
                "yixin.ui.task_center_page.upload_manual_candidates",
                return_value=1,
            ) as upload_manual,
            patch(
                "yixin.ui.task_center_page.QMessageBox.information"
            ),
            patch.object(center, "refresh"),
        ):
            center._upload_manual()
        upload_manual.assert_called_once()
        self.assertEqual(upload_manual.call_args.args[3].id, task.id)
        self.assertEqual(
            open_files.call_args.args[2],
            str(upload_history.resolve()),
        )
        self.assertNotIn("options", open_files.call_args.kwargs)
        self.assertEqual(
            center.app_settings.input_folder(INPUT_MANUAL_UPLOAD),
            ps_image.parent.resolve(),
        )
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
        snapshot = center.local_store.load_attempt_snapshot(7)
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot), 3)
        center.deleteLater()

    def test_task_center_uses_source_strip_and_current_results(self) -> None:
        write_task_images(self.root)
        task = make_task("working")
        product_root = self.root / task.project_relative_root
        garment_assets = tuple(
            replace(
                asset,
                content_hash=hashlib.sha256(
                    (
                        product_root / asset.relative_path
                    ).read_bytes()
                ).hexdigest(),
            )
            for asset in task.garment_assets
        )
        model_hash = hashlib.sha256(
            (product_root / task.model_path).read_bytes()
        ).hexdigest()
        task = replace(
            task,
            garment_assets=garment_assets,
            garment_hash=garment_assets[0].content_hash,
            model_hash=model_hash,
        )
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
        self.repository.list_templates = lambda _actor: [
            {
                "scope": "shared",
                "name": "内置·基础服装替换",
                "prompt": "内置基础提示词",
            }
        ]
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
        center.resize(1480, 900)
        center.show()
        self.app.processEvents()
        center._sync_task_splitter_to_generation()
        self.app.processEvents()
        self.assertFalse(hasattr(center, "task_detail"))
        self.assertEqual(center.source_title.text(), "子任务参考图")
        self.assertEqual(
            center.task_table.selectionMode(),
            QAbstractItemView.SelectionMode.ExtendedSelection,
        )
        self.assertEqual(
            center.subtask_hint.text(),
            "按住 Ctrl 多选，按住 Shift 连续选择",
        )
        self.assertTrue(center.local_source_hint.isHidden())
        center._sync_task_splitter_to_generation()
        self.assertEqual(
            center.generation_splitter.sizes(),
            center.task_splitter.sizes(),
        )
        self.assertLessEqual(
            abs(
                center.source_strip.mapTo(center, QPoint(0, 0)).y()
                - center.gallery.list_widget.mapTo(
                    center, QPoint(0, 0)
                ).y()
            ),
            8,
        )
        self.assertLessEqual(
            abs(
                center.task_table.mapTo(center, QPoint(0, 0)).y()
                - center.queue_table.mapTo(center, QPoint(0, 0)).y()
            ),
            12,
        )
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
        center.source_strip.setCurrentRow(0)
        self.assertEqual(center.project_table.rowCount(), 1)
        self.assertEqual(center.task_table.rowCount(), 1)
        self.assertEqual(
            [
                center.project_table.horizontalHeaderItem(i).text()
                for i in range(center.project_table.columnCount())
            ],
            ["任务编码", "货号", "子任务进度", "状态", "创建时间"],
        )
        self.assertEqual(center.project_table.item(0, 2).text(), "0/1")
        self.assertTrue(center.project_table.isSortingEnabled())
        self.assertTrue(center.task_table.isSortingEnabled())
        self.assertEqual(
            center.queue_table.horizontalHeaderItem(4).text(),
            "结果数",
        )
        self.assertIs(
            center.generation_splitter.widget(1).findChild(QTableWidget),
            center.queue_table,
        )
        self.assertEqual(center.project_filter_combo.count(), 2)
        self.assertEqual(
            center.project_filter_combo.itemText(1),
            "春季项目",
        )
        center.product_search.setText("不存在")
        self.app.processEvents()
        self.assertEqual(center.project_table.rowCount(), 0)
        center.product_search.clear()
        self.app.processEvents()
        self.assertEqual(center.project_table.rowCount(), 1)
        self.assertFalse(hasattr(center, "source_role_combo"))
        self.assertFalse(hasattr(center, "source_preview"))
        self.assertEqual(center.result_title.text(), "当前任务生成结果")
        self.assertEqual(
            [
                center.add_source_files_button.text(),
                center.add_source_folder_button.text(),
                center.add_source_library_button.text(),
                center.crop_source_button.text(),
                center.restore_source_button.text(),
                center.remove_source_button.text(),
                center.clear_sources_button.text(),
            ],
            [
                "添加图片",
                "添加文件夹",
                "从作品库添加",
                "裁剪",
                "恢复",
                "移除所选",
                "清空",
            ],
        )
        self.assertIsNone(center.template_combo.currentData())
        self.assertEqual(
            center.prompt_edit.toPlainText(),
            task.prompt_text,
        )
        center.template_combo.setCurrentIndex(1)
        self.assertEqual(
            center.prompt_edit.toPlainText(),
            "内置基础提示词",
        )
        self.assertEqual(center.gallery.list_widget.count(), 1)
        self.assertEqual(
            center.gallery.preview.current_path,
            result_path.resolve(),
        )
        extra_garment = self.root / "额外衣服.png"
        Image.new("RGB", (30, 40), "yellow").save(extra_garment)
        center.source_strip.setCurrentRow(0)
        with patch(
            "yixin.ui.task_center_page.QFileDialog.getOpenFileNames",
            return_value=([str(extra_garment)], ""),
        ):
            center._add_source_files()
        self.assertEqual(len(center.local_state.garments), 3)
        self.assertEqual(center.source_strip.count(), 4)
        self.assertFalse(center.local_source_hint.isHidden())
        extra_model = self.root / "额外模特.png"
        Image.new("RGB", (30, 40), "cyan").save(extra_model)
        center.source_strip.setCurrentRow(
            center.source_strip.count() - 1
        )
        with patch(
            "yixin.ui.task_center_page.QFileDialog.getOpenFileNames",
            return_value=([str(extra_model)], ""),
        ):
            center._add_source_files()
        self.assertEqual(
            center.local_state.model.path.read_bytes(),
            extra_model.read_bytes(),
        )
        dropped_folder = self.root / "拖入任务素材"
        dropped_folder.mkdir()
        Image.new("RGB", (30, 40), "black").save(
            dropped_folder / "拖入衣服.png"
        )
        center.source_strip.setCurrentRow(0)
        center._handle_source_drop([dropped_folder])
        self.assertEqual(len(center.local_state.garments), 4)
        restored = TaskCenterPage(
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
        self.assertIsNotNone(restored.local_state)
        self.assertEqual(len(restored.local_state.garments), 4)
        with patch(
            "yixin.ui.task_center_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            center._clear_sources()
        self.assertEqual(center.local_state.garments, ())
        self.assertIsNone(center.local_state.model)
        self.assertFalse(center.generate_button.isEnabled())
        center._restore_source()
        self.assertEqual(len(center.local_state.garments), 2)
        self.assertIsNotNone(center.local_state.model)
        restored.deleteLater()
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

    def test_task_center_local_source_permissions(self) -> None:
        write_task_images(self.root)
        ai_actor = AuthUser(4, "ai", "AI制作Amy", "ai")
        task = make_task("working")
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            task
        ]
        center = TaskCenterPage(
            self.repository,
            self.resolver,
            ai_actor,
            NanoBananaClient(lambda: "test-key"),
            type(
                "KeyStore",
                (),
                {"load": staticmethod(lambda: "test-key")},
            )(),
            FileStore(AppPaths(self.root / "permission-data")),
            AppSettings(Database(self.root / "permission.db")),
        )
        self.assertTrue(center.add_source_files_button.isEnabled())
        center.deleteLater()

        task = make_task("awaiting_acceptance")
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            task
        ]
        pending = TaskCenterPage(
            self.repository,
            self.resolver,
            ai_actor,
            NanoBananaClient(lambda: "test-key"),
            type(
                "KeyStore",
                (),
                {"load": staticmethod(lambda: "test-key")},
            )(),
            FileStore(AppPaths(self.root / "permission-pending")),
            AppSettings(Database(self.root / "permission-pending.db")),
        )
        self.assertFalse(pending.add_source_files_button.isEnabled())
        pending.deleteLater()

    def test_task_center_crop_updates_unstarted_queue_snapshot(self) -> None:
        write_task_images(self.root)
        task = make_task("working")
        queued = GenerationAttempt(
            id=77,
            task_id=task.id,
            attempt_no=1,
            operator_id=self.actor.id,
            model="gemini-3.1-flash-image",
            prompt_text="等待执行",
            aspect_ratio="3:4",
            resolution="2K",
            n=1,
            status=ATTEMPT_QUEUED,
            source_revision=task.source_revision,
            unit_price=Decimal("0"),
            generated_image_count=0,
            created_at=datetime.now(),
        )
        self.repository.queued_attempts = [queued]
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            task
        ]
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
            FileStore(AppPaths(self.root / "crop-queue-data")),
            AppSettings(Database(self.root / "crop-queue.db")),
        )
        original_hash = center.local_state.garments[0].content_hash
        center.source_strip.setCurrentRow(0)
        dialog = type(
            "CropDialogStub",
            (),
            {
                "DialogCode": QDialog.DialogCode,
                "exec": lambda _self: QDialog.DialogCode.Accepted,
                "crop_box": lambda _self: (0, 0, 10, 10),
            },
        )()
        with patch(
            "yixin.ui.task_center_page.CropDialog",
            return_value=dialog,
        ):
            center._crop_source()
        snapshot = center.local_store.load_attempt_snapshot(77)
        self.assertIsNotNone(snapshot)
        self.assertNotEqual(snapshot[0].content_hash, original_hash)
        center.deleteLater()

    def test_task_center_batch_submits_selected_subtasks(self) -> None:
        write_task_images(self.root)
        first = replace(
            make_task("working"),
            assignee_id=self.actor.id,
            assignee_name=self.actor.display_name,
        )
        second = replace(
            first,
            id=12,
            task_code="春季项目-123-001-002",
        )
        product_root = self.root / first.project_relative_root
        results_by_task = {}
        for task, result_id, color in (
            (first, 101, "orange"),
            (second, 102, "purple"),
        ):
            result_path = product_root / "AI成品" / f"{result_id}.png"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (40, 50), color).save(result_path)
            results_by_task[task.id] = [
                GenerationResult(
                    id=result_id,
                    attempt_id=result_id,
                    result_index=0,
                    relative_path=result_path.relative_to(
                        product_root
                    ).as_posix(),
                    thumbnail_relative_path=result_path.relative_to(
                        product_root
                    ).as_posix(),
                    content_hash=f"{result_id:064d}",
                    mime="image/png",
                    width=40,
                    height=50,
                    is_final=False,
                )
            ]
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            first,
            second,
        ]
        self.repository.list_results = lambda task_id: list(
            results_by_task[task_id]
        )
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
            FileStore(AppPaths(self.root / "batch-submit-data")),
            AppSettings(Database(self.root / "batch-submit.db")),
        )
        first_task_id = center.current_task.id
        first_result_id = results_by_task[first_task_id][0].id
        center.gallery.set_selected_result_ids((first_result_id,))
        center._selection_changed()
        other_row = next(
            row
            for row in range(center.task_table.rowCount())
            if int(
                center.task_table.item(row, 0).data(
                    Qt.ItemDataRole.UserRole
                )
            )
            != first_task_id
        )
        center.task_table.setCurrentCell(
            other_row,
            0,
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        center._task_selected()
        second_task_id = center.current_task.id
        second_result_id = results_by_task[second_task_id][0].id
        center.gallery.set_selected_result_ids((second_result_id,))
        center._selection_changed()
        self.assertCountEqual(center._selected_task_ids(), [11, 12])
        with (
            patch(
                "yixin.ui.task_center_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(center, "refresh"),
        ):
            center._submit()
        submitted = {
            request[0]: request[2]["final_result_ids"]
            for request in self.repository.submission_requests
        }
        self.assertEqual(
            submitted,
            {
                first_task_id: (first_result_id,),
                second_task_id: (second_result_id,),
            },
        )
        center.deleteLater()

    def test_task_center_default_prompt_and_two_column_parameters(self) -> None:
        write_task_images(self.root)
        task = replace(make_task("working"), prompt_text="")
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            task
        ]
        self.repository.list_templates = lambda _actor: [
            {
                "scope": "shared",
                "name": "内置·基础服装替换",
                "prompt": BUILTIN_PROMPT_TEMPLATES[0][1],
            }
        ]
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
            FileStore(AppPaths(self.root / "default-prompt-data")),
            AppSettings(Database(self.root / "default-prompt.db")),
        )
        self.assertEqual(
            center.prompt_edit.toPlainText(),
            BUILTIN_PROMPT_TEMPLATES[0][1],
        )
        self.assertEqual(
            center.template_combo.currentText(),
            "选择提示词模板",
        )
        self.assertEqual(center.parameters_grid.count(), 4)
        self.assertEqual(
            center.parameters_grid.getItemPosition(3)[:2],
            (0, 3),
        )
        center.deleteLater()

    def test_task_center_groups_progress_and_filters(self) -> None:
        write_task_images(self.root)
        working = make_task("working")
        approved = replace(
            working,
            id=12,
            task_code="春季项目-123-001-002",
            status="approved",
        )
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            working,
            approved,
        ]
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
            FileStore(AppPaths(self.root / "group-data")),
            AppSettings(Database(self.root / "group.db")),
        )
        self.assertEqual(center.project_table.rowCount(), 1)
        self.assertEqual(center.project_table.item(0, 2).text(), "1/2")
        self.assertEqual(center.task_table.rowCount(), 1)
        center.include_finished.setChecked(True)
        self.app.processEvents()
        self.assertEqual(center.task_table.rowCount(), 2)
        center.product_search.setText("不存在")
        self.assertEqual(center.project_table.rowCount(), 0)
        center.product_search.clear()
        working_date = utc_naive_to_local(working.created_at).date()
        working_qdate = QDate(
            working_date.year,
            working_date.month,
            working_date.day,
        )
        center.center_start_date.setDate(working_qdate)
        center.center_end_date.setDate(working_qdate)
        self.assertEqual(center.project_table.rowCount(), 1)
        center.deleteLater()

    def test_task_center_skip_remaining_marks_project_tasks(self) -> None:
        write_task_images(self.root)
        working = make_task("working")
        remaining = [
            replace(
                working,
                id=20 + index,
                task_code=f"春季项目-123-001-{index + 2:03d}",
                status="available",
                assignee_id=None,
                assignee_name="",
            )
            for index in range(3)
        ]
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            working,
            *remaining,
        ]
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
            FileStore(AppPaths(self.root / "skip-data")),
            AppSettings(Database(self.root / "skip.db")),
        )
        button_names = [
            center.retry_button.text(),
            center.save_button.text(),
            center.upload_button.text(),
            center.compare_button.text(),
            center.submit_button.text(),
            center.skip_button.text(),
        ]
        self.assertEqual(button_names[-2:], ["提交验收", "剩余不制作"])
        with (
            patch(
                "yixin.ui.task_center_page.QInputDialog.getMultiLineText",
                return_value=("实际只需完成一张", True),
            ),
            patch(
                "yixin.ui.task_center_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch(
                "yixin.ui.task_center_page.QMessageBox.information"
            ),
        ):
            center._skip_remaining()
        self.assertEqual(len(self.repository.skip_requests), 1)
        self.assertEqual(
            self.repository.skip_requests[0][2],
            "实际只需完成一张",
        )
        center.deleteLater()

    def test_task_center_completed_with_skipped_tasks(self) -> None:
        write_task_images(self.root)
        approved = make_task("approved")
        skipped = replace(
            approved,
            id=31,
            task_code="春季项目-123-001-009",
            status="skipped",
            assignee_id=None,
            assignee_name="",
        )
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            approved,
            skipped,
        ]
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
            FileStore(AppPaths(self.root / "skipped-data")),
            AppSettings(Database(self.root / "skipped.db")),
        )
        center.include_finished.setChecked(True)
        self.app.processEvents()
        self.assertEqual(center.project_table.item(0, 3).text(), "已完成")
        self.assertIn(
            "无需制作 1",
            center.project_table.item(0, 2).text(),
        )
        self.assertEqual(center.task_table.rowCount(), 2)
        center.deleteLater()

    def test_project_history_filters_and_shows_attempt_results(self) -> None:
        write_task_images(self.root)
        task = make_task("working")
        product_root = self.root / task.project_relative_root
        result_path = (
            product_root
            / "AI成品"
            / task.task_code
            / "attempt_001"
            / "result.png"
        )
        result_path.parent.mkdir(parents=True)
        Image.new("RGB", (30, 40), "orange").save(result_path)
        result = GenerationResult(
            id=30,
            attempt_id=40,
            result_index=0,
            relative_path=result_path.relative_to(product_root).as_posix(),
            thumbnail_relative_path=result_path.relative_to(
                product_root
            ).as_posix(),
            content_hash="b" * 64,
            mime="image/png",
            width=30,
            height=40,
            is_final=False,
        )
        self.repository.tasks_by_id[task.id] = task
        self.repository.attempt_results[40] = [result]
        self.repository.project_attempt_history = [
            {
                "id": 40,
                "task_id": task.id,
                "attempt_no": 1,
                "project_code": task.project_code,
                "task_code": task.task_code,
                "product_code": task.product_code,
                "project_name": task.project_name,
                "operator_name": "AI制作Amy",
                "model": "gemini-3.1-flash-image",
                "status": "succeeded",
                "prompt_text": "项目历史提示词",
                "created_at": datetime.now(),
                "result_count": 1,
            }
        ]
        page = ProjectHistoryPage(
            self.repository,
            self.resolver,
            self.actor,
        )
        self.assertEqual(page.table.rowCount(), 1)
        self.assertEqual(page.table.item(0, 1).text(), task.task_code)
        self.assertEqual(page.gallery.list_widget.count(), 1)
        self.assertEqual(
            page.gallery.preview.current_path,
            result_path.resolve(),
        )
        page.product_search.setText("不存在")
        self.assertEqual(page.table.rowCount(), 0)
        page.product_search.clear()
        self.assertEqual(page.table.rowCount(), 1)
        self.assertTrue(page.table.isSortingEnabled())
        page.deleteLater()
        database = Database(self.root / "history-tabs.db")
        database.initialize()
        paths = AppPaths(self.root / "history-tabs-data")
        paths.ensure()
        client = NanoBananaClient(lambda: "test-key")
        history = HistoryTabsPage(
            database,
            LibraryService(database, FileStore(paths), paths),
            TaskWorker(database, client, FileStore(paths)),
            self.repository,
            self.resolver,
            self.actor,
        )
        self.assertEqual(history.tabs.tabText(0), "项目任务")
        self.assertEqual(history.tabs.tabText(1), "普通创作")
        history.deleteLater()


if __name__ == "__main__":
    unittest.main()
