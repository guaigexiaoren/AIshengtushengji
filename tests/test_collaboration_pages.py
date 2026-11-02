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
        QGraphicsView,
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
        ATTEMPT_SUCCEEDED,
        ROLE_ADMIN,
        ROLE_OPERATIONS,
        ApiProvider,
        AuthUser,
        GenerationAttempt,
        GenerationResult,
        ProjectAssetInput,
        ProjectRecord,
        ProjectTask,
    )
    from yixin.constants import (
        BUILTIN_PROMPT_TEMPLATES,
        PROTOCOL_OPENAI_CHAT_IMAGE,
        PROTOCOL_OPENAI_IMAGES,
        TASK_SUCCESS,
    )
    from yixin.database import Database
    from yixin.library import LibraryService
    from yixin.models import ImageRequest
    from yixin.paging import PageRequest, PageResult, TaskQuery
    from yixin.paths import AppPaths
    from yixin.security import SecretStore
    from yixin.settings import INPUT_MANUAL_UPLOAD, AppSettings
    from yixin.storage import FileStore
    from yixin.time_utils import utc_naive_to_local
    from yixin.ui.admin_page import (
        AddUserDialog,
        AdminPage,
        ApiProviderDialog,
        EditUserDialog,
    )
    from yixin.ui.collaboration_login import LoginDialog
    from yixin.ui.dialogs import SettingsDialog
    from yixin.ui.main_window import MainWindow, SidebarNav
    from yixin.ui.submission_list_page import SubmissionListPage
    from yixin.ui.subtask_source_dialog import SubtaskSourceDialog
    from yixin.ui.project_history_page import ProjectHistoryPage
    from yixin.ui.unified_history_page import (
        UnifiedHistoryPage,
    )
    from yixin.ui.image_preview import (
        LargeImageDialog,
    )
    from yixin.ui.date_filter import OptionalDateEdit
    from yixin.ui.task_center_page import TaskCenterPage
    from yixin.ui.task_assignment_page import TaskAssignmentPage
    from yixin.ui.ai_generation_page import AIGenerationPage
    from yixin.ui.task_create_page import (
        CheckableHeaderView,
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
        self.project_attempts = []
        self.review_requests = []
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
            "assign_tasks",
            "ai_generate",
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

    def list_assignment_projects(self, _actor, **_kwargs):
        return []

    def list_ai_generation_tasks(self, _actor, **_kwargs):
        return []

    def claim_projects(self, _project_ids, _actor):
        return ()

    def split_and_queue_generations(self, _task_id, _actor, **_kwargs):
        return []

    def mark_task_skipped(self, _task_id, _actor):
        return None

    def list_attempts(self, _task_id):
        return []

    def list_project_attempts(self, _project_id):
        return list(self.project_attempts)

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

    def review_task(self, task_id, actor, *, approved, reason=""):
        self.review_requests.append(
            (task_id, actor, approved, reason)
        )

    def api_key_statuses(self):
        return {}

    def api_key_statuses_by_provider(self):
        return {}

    def api_key_status(self, _user_id, _provider_id):
        return None

    def list_api_providers(self, **_kwargs):
        return []

    def list_provider_models(self, _provider_id, **_kwargs):
        return []

    def list_provider_routes(self, _model, **_kwargs):
        return []

    def project_progress_counts(self, project_ids):
        return {int(project_id): (0, 0) for project_id in project_ids}

    def cancel_queued_generation_attempt(self, _attempt_id, _actor):
        return None

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

    def load_actor_api_key(self, _actor, **_kwargs):
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

    def queue_generation_attempt(self, task_id, actor, **kwargs):
        self.generation_requests.append((task_id, actor, kwargs))
        return GenerationAttempt(
            id=8,
            task_id=task_id,
            attempt_no=2,
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

    def test_ai_submit_removes_pair_controls_and_enlarges_assets(self) -> None:
        page = TaskCreatePage(
            self.repository,
            self.resolver,
            self.actor,
            auto_load=False,
        )
        self.assertFalse(hasattr(page, "mode_combo"))
        self.assertFalse(hasattr(page, "pair_preview"))
        self.assertEqual(page.garment_list.iconSize().width(), 128)
        self.assertGreaterEqual(page.garment_list.minimumHeight(), 240)
        page.deleteLater()

    def test_submission_records_are_read_only_and_hide_expanded_parents(
        self,
    ) -> None:
        project = make_project()
        available = replace(make_task("available"), id=11)
        approved = replace(
            make_task("approved"),
            id=12,
            task_code="春季项目-123-001-002",
        )
        expanded = replace(
            make_task("expanded"),
            id=13,
            task_code="春季项目-123-001-003",
        )
        self.repository.list_projects = lambda **_kwargs: [project]
        self.repository.list_board_tasks = (
            lambda _actor, **_kwargs: [available, approved, expanded]
        )
        page = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "records",
        )
        self.assertEqual(page.project_table.rowCount(), 1)
        self.assertTrue(page.project_table.isColumnHidden(6))
        self.assertEqual(page.task_table.rowCount(), 2)
        self.assertFalse(page.urgent_button.isVisible())
        self.assertFalse(page.review_actions.isVisible())
        self.assertFalse(page.result_panel.isHidden())
        self.assertIs(
            page.result_panel,
            page.dual_preview.result_pane,
        )
        self.assertIs(
            page.reference_panel,
            page.dual_preview.reference_pane,
        )
        self.assertFalse(page._can_edit_task(available))
        page.deleteLater()

    def test_assignment_page_multi_select_and_claim(self) -> None:
        write_task_images(self.root)
        project = make_project()
        task = replace(make_task("available"), assignee_id=None)
        self.repository.list_assignment_projects = (
            lambda _actor, **_kwargs: [project]
        )
        self.repository.list_project_tasks = lambda _project_id: [task]
        requested: list[list[int]] = []

        def claim(project_ids, _actor):
            requested.append(list(project_ids))
            return tuple(project_ids)

        self.repository.claim_projects = claim
        page = TaskAssignmentPage(
            self.repository,
            self.resolver,
            self.actor,
            auto_load=False,
        )
        page.refresh()
        self.assertEqual(page.project_table.rowCount(), 1)
        self.assertEqual(page.task_table.rowCount(), 1)
        self.assertEqual(page.source_strip.count(), 3)
        page.project_table.selectRow(0)
        with patch(
            "yixin.ui.task_assignment_page.QMessageBox.information"
        ):
            page._claim_selected()
        self.assertEqual(requested, [[project.id]])
        page.deleteLater()

    def test_assignment_page_async_pages_projects_and_tasks(self) -> None:
        write_task_images(self.root)
        project = make_project()
        task = replace(make_task("available"), assignee_id=None)
        self.repository.list_assignment_projects_page = (
            lambda _actor, _query, page: PageResult(
                [project],
                1,
                page.page,
                page.page_size,
            )
        )
        self.repository.list_board_tasks_for_project_page = (
            lambda project_id, _actor, _statuses, _query, page: PageResult(
                [task],
                1,
                page.page,
                page.page_size,
            )
        )
        page = TaskAssignmentPage(
            self.repository,
            self.resolver,
            self.actor,
            auto_load=False,
            async_mode=True,
        )
        page.refresh()
        loaded = False
        for _ in range(100):
            self.app.processEvents()
            loaded = (
                page.project_table.rowCount() == 1
                and page.task_table.rowCount() == 1
            )
            if loaded:
                break
            QTest.qWait(10)
        self.assertTrue(loaded)
        self.assertIn("共 1 条", page.project_pagination.summary.text())
        self.assertIn("共 1 条", page.task_pagination.summary.text())
        page.deleteLater()

    def test_ai_generation_page_loads_subtask_and_toggles_skip(self) -> None:
        write_task_images(self.root)
        task = make_task("claimed")
        self.repository.list_ai_generation_tasks = (
            lambda _actor, **_kwargs: [task]
        )
        self.repository.list_queued_attempts = lambda _actor_id: []
        key_store = type(
            "KeyStore",
            (),
            {"load": staticmethod(lambda: "sk-test-123456")},
        )()
        paths = AppPaths(self.root / "ai-page-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
        client = NanoBananaClient(lambda: "sk-test-123456")
        worker = TaskWorker(database, client, file_store)
        page = AIGenerationPage(
            self.repository,
            self.resolver,
            AuthUser(4, "ai_amy", "AI Amy", "ai"),
            client,
            key_store,
            database,
            file_store,
            LibraryService(database, file_store, paths),
            worker,
            key_store,
            auto_load=False,
        )
        page.refresh()
        self.assertEqual(page.task_selector.count(), 2)
        self.assertEqual(len(page.garment_assets), 2)
        self.assertEqual(len(page.person_assets), 1)
        self.assertEqual(len(page.pairs), 1)
        self.assertIsNotNone(page.provider_combo)
        self.assertNotEqual(
            page.template_combo.parentWidget(),
            page.prompt_edit.parentWidget(),
        )
        skip_buttons = [
            button
            for button in page.findChildren(QPushButton)
            if button.text() == "不做图" and button.isVisibleTo(page)
        ]
        self.assertEqual(len(skip_buttons), 2)
        button_texts = {
            button.text() for button in page.findChildren(QPushButton)
        }
        self.assertNotIn("上传PS成品", button_texts)
        self.assertNotIn("提交验收", button_texts)
        self.assertNotIn("验收通过", button_texts)
        card_skip = next(
            button
            for button in skip_buttons
            if button is not page.bottom_skip_button
        )
        card_skip.click()
        self.assertTrue(page.pairs[0].skipped)
        self.assertEqual(
            page.selected_pair_key,
            page.pairs[0].key,
        )
        self.assertEqual(page.bottom_skip_button.text(), "恢复制作")
        page.bottom_skip_button.click()
        self.assertFalse(page.pairs[0].skipped)
        page.bottom_skip_button.click()
        self.assertTrue(page.pairs[0].skipped)
        page.deleteLater()

    def test_ai_generation_page_async_searches_first_hundred_tasks(
        self,
    ) -> None:
        write_task_images(self.root)
        task = make_task("claimed")
        queries = []
        self.repository.list_ai_generation_tasks_page = (
            lambda _actor, query, page: (
                queries.append((query, page))
                or PageResult([task], 1, page.page, page.page_size)
            )
        )
        paths = AppPaths(self.root / "ai-async-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
        client = NanoBananaClient(lambda: "test-key")
        page = AIGenerationPage(
            self.repository,
            self.resolver,
            self.actor,
            client,
            SecretStore(paths.api_key_file),
            database,
            file_store,
            LibraryService(database, file_store, paths),
            TaskWorker(database, client, file_store),
            SecretStore(paths.api_key_file),
            auto_load=False,
            async_mode=True,
        )
        page.refresh()
        for _ in range(100):
            self.app.processEvents()
            if page.task_selector.count() == 2:
                break
            QTest.qWait(10)
        self.assertEqual(page.task_selector.count(), 2)
        self.assertEqual(queries[0][1].page_size, 100)
        page.task_search_edit.setText("春季项目")
        for _ in range(100):
            self.app.processEvents()
            if len(queries) >= 2:
                break
            QTest.qWait(10)
        self.assertEqual(queries[-1][0].keyword, "春季项目")
        page.deleteLater()

    def test_ai_generation_page_splits_each_pair_into_child_task(self) -> None:
        write_task_images(self.root)
        task = make_task("claimed")
        self.repository.list_ai_generation_tasks = (
            lambda _actor, **_kwargs: [task]
        )
        self.repository.list_queued_attempts = lambda _actor_id: []
        self.repository.list_api_providers = lambda **_kwargs: [
            ApiProvider(
                id=3,
                provider_code="a6api",
                display_name="A6API",
                base_url="https://api.a6api.vip/v1",
                default_protocol=PROTOCOL_OPENAI_IMAGES,
                enabled=True,
                priority=30,
            )
        ]
        self.repository.project_progress_counts = (
            lambda _ids: {task.project_id: (2, 5)}
        )
        captured: list[tuple] = []

        def split(task_id, actor, *, pairs):
            captured.append((task_id, actor.id, tuple(pairs)))
            return ()

        self.repository.split_and_queue_generations = split
        key_store = type(
            "KeyStore",
            (),
            {"load": staticmethod(lambda: "sk-test-123456")},
        )()
        paths = AppPaths(self.root / "ai-split-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
        client = NanoBananaClient(lambda: "sk-test-123456")
        page = AIGenerationPage(
            self.repository,
            self.resolver,
            AuthUser(4, "ai_amy", "AI Amy", "ai"),
            client,
            key_store,
            database,
            file_store,
            LibraryService(database, file_store, paths),
            TaskWorker(database, client, file_store),
            key_store,
            auto_load=False,
        )
        page.refresh()
        self.assertEqual(page.provider_combo.count(), 2)
        self.assertIn("主任务 2/5", page.task_selector.itemText(1))
        self.assertIn("待制作", page.task_selector.itemText(1))
        page.provider_combo.setCurrentIndex(1)
        page._select_pair(page.pairs[0])
        page.bottom_skip_button.click()
        page._enqueue_all()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], task.id)
        specs = captured[0][2]
        self.assertEqual(len(specs), 1)
        self.assertEqual(len(specs[0].garment_hashes), 2)
        self.assertTrue(specs[0].skipped)
        self.assertEqual(
            specs[0].requested_provider_code,
            "a6api",
        )
        page.deleteLater()

    def test_ai_generation_page_retries_failed_child_without_split(self) -> None:
        write_task_images(self.root)
        task = replace(
            make_task("claimed"),
            id=21,
            task_code="春季项目-123-001-003",
            parent_task_id=11,
        )
        self.repository.list_ai_generation_tasks = (
            lambda _actor, **_kwargs: [task]
        )
        self.repository.list_queued_attempts = lambda _actor_id: []
        key_store = type(
            "KeyStore",
            (),
            {"load": staticmethod(lambda: "sk-test-123456")},
        )()
        paths = AppPaths(self.root / "ai-retry-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
        client = NanoBananaClient(lambda: "sk-test-123456")
        page = AIGenerationPage(
            self.repository,
            self.resolver,
            AuthUser(4, "ai_amy", "AI Amy", "ai"),
            client,
            key_store,
            database,
            file_store,
            LibraryService(database, file_store, paths),
            TaskWorker(database, client, file_store),
            key_store,
            auto_load=False,
        )
        page.refresh()
        page._enqueue_all()
        self.assertEqual(len(self.repository.generation_requests), 1)
        task_id, _actor, _kwargs = self.repository.generation_requests[0]
        self.assertEqual(task_id, task.id)
        page.deleteLater()

    def test_ai_generation_page_prepares_completed_history_retry(self) -> None:
        write_task_images(self.root)
        task = replace(
            make_task("approved"),
            id=31,
            task_code="春季项目-123-001-005",
            parent_task_id=11,
        )
        attempt = GenerationAttempt(
            id=41,
            task_id=task.id,
            attempt_no=3,
            operator_id=4,
            model="gemini-3.1-flash-image",
            prompt_text="历史重试提示词",
            aspect_ratio="4:3",
            resolution="1K",
            n=2,
            status=ATTEMPT_SUCCEEDED,
            source_revision=1,
            unit_price=Decimal("1.00"),
            generated_image_count=1,
            provider_name="A6API",
            requested_provider_code="a6api",
            requested_provider_name="A6API",
            created_at=datetime.now(),
        )
        self.repository.tasks_by_id[task.id] = task
        self.repository.list_attempts = lambda _task_id: [attempt]
        self.repository.list_ai_generation_tasks = (
            lambda _actor, **_kwargs: []
        )
        self.repository.list_queued_attempts = lambda _actor_id: []
        self.repository.list_api_providers = lambda **_kwargs: [
            ApiProvider(
                id=3,
                provider_code="a6api",
                display_name="A6API",
                base_url="https://api.a6api.vip/v1",
                default_protocol=PROTOCOL_OPENAI_IMAGES,
                enabled=True,
                priority=30,
            )
        ]
        paths = AppPaths(self.root / "history-retry-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        store = FileStore(paths)
        client = NanoBananaClient(lambda: "test-key")
        key_store = type(
            "KeyStore",
            (),
            {"load": staticmethod(lambda: "sk-test-123456")},
        )()
        page = AIGenerationPage(
            self.repository,
            self.resolver,
            AuthUser(4, "ai_amy", "AI Amy", "ai"),
            client,
            key_store,
            database,
            store,
            LibraryService(database, store, paths),
            TaskWorker(database, client, store),
            key_store,
            auto_load=False,
        )
        page.refresh()
        page.prepare_history_retry(task.id, attempt.id)
        self.assertEqual(page.current_task.id, task.id)
        self.assertEqual(
            page.prompt_edit.toPlainText(),
            "历史重试提示词",
        )
        self.assertEqual(page.provider_combo.currentData(), "a6api")
        self.assertEqual(page.count_spin.value(), 2)
        page._enqueue_all()
        self.assertEqual(len(self.repository.generation_requests), 1)
        _task_id, _actor, kwargs = self.repository.generation_requests[0]
        self.assertTrue(kwargs["allow_reopen"])
        self.assertEqual(kwargs["requested_provider_code"], "a6api")
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
            "AI提交",
            "提交记录",
            "待分配",
            "AI生图",
            "普通创作",
            "批量换装",
            "历史记录",
            "作品库",
            "管理控制台",
        ]
        nav = SidebarNav(
            labels,
            group_before={
                0: "AI图提交",
                2: "任务中心",
                4: "创造中心",
            },
            submenu_indices={0, 1, 2, 3, 4, 5},
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
                for index in (0, 1, 2, 3, 4, 5)
            )
        )
        self.assertFalse(nav._buttons[6].property("submenu"))
        group_label = nav.findChild(QLabel, "NavGroup")
        self.assertIsNotNone(group_label)
        self.assertEqual(
            group_label.font().pixelSize(),
            nav._buttons[6].font().pixelSize(),
        )
        self.assertEqual(
            [
                label.text()
                for label in nav.findChildren(QLabel, "NavGroup")
            ],
            ["AI图提交", "任务中心", "创造中心"],
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

    def test_admin_api_platform_tab_lists_provider_routes(self) -> None:
        provider = ApiProvider(
            id=3,
            provider_code="a6api",
            display_name="A6API",
            base_url="https://api.a6api.vip/v1",
            default_protocol=PROTOCOL_OPENAI_IMAGES,
            enabled=True,
            priority=30,
        )
        self.repository.list_api_providers = lambda **_kwargs: [provider]
        self.repository.list_provider_models = (
            lambda *_args, **_kwargs: [{"logical_model": "model"}]
        )
        page = AdminPage(self.repository, self.actor)
        self.assertEqual(page.provider_table.rowCount(), 1)
        self.assertEqual(page.provider_table.item(0, 0).text(), "A6API")
        self.assertEqual(page.provider_table.item(0, 5).text(), "1 个")
        page.deleteLater()

    def test_admin_api_provider_dialog_edits_model_mappings(self) -> None:
        provider = ApiProvider(
            id=2,
            provider_code="88api",
            display_name="88API",
            base_url="https://api.88api.ai/v1",
            default_protocol=PROTOCOL_OPENAI_CHAT_IMAGE,
            enabled=True,
            priority=20,
        )
        dialog = ApiProviderDialog(
            self.repository,
            self.actor,
            provider,
            [],
        )
        self.assertEqual(dialog.model_table.rowCount(), 3)
        self.assertEqual(
            dialog.model_table.cellWidget(0, 2).currentData(),
            PROTOCOL_OPENAI_CHAT_IMAGE,
        )
        self.assertIn(
            "gemini-nano-banana-2.1",
            dialog.model_table.cellWidget(0, 1).text(),
        )
        dialog.deleteLater()

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
            ("new_task",),
            (("internal", "春季项目"),),
            self.repository.list_shared_projects(),
        )
        self.assertFalse(
            dialog.permissions.menu_checks["assign_tasks"].isEnabled()
        )
        self.assertFalse(
            dialog.permissions.menu_checks["ai_generate"].isEnabled()
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
                "assign_tasks"
            ].isEnabled()
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
            ["AI提交", "提交记录"],
        )
        with patch.object(window.new_task_page, "refresh") as refresh:
            window.show()
            self.app.processEvents()
            QTest.qWait(20)
            refresh.assert_called_once_with()
        window.close()
        window.deleteLater()

    def test_main_window_creates_pages_only_when_first_visited(self) -> None:
        paths = AppPaths(self.root / "lazy-window-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
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
            actor=self.actor,
            lazy_pages=True,
        )
        self.assertTrue(hasattr(window, "new_task_page"))
        self.assertFalse(hasattr(window, "create_page"))
        self.assertFalse(hasattr(window, "history_page"))
        create_index = next(
            index
            for index in range(window.nav_list.count())
            if window.nav_list.item(index).text() == "普通创作"
        )
        window.nav_list.setCurrentRow(create_index)
        self.app.processEvents()
        self.assertTrue(hasattr(window, "create_page"))
        self.assertIs(
            window.stack.currentWidget(),
            window.create_page,
        )
        self.assertFalse(hasattr(window, "history_page"))
        window.close()
        window.deleteLater()

    def test_lazy_main_window_uses_cache_until_page_is_dirty(self) -> None:
        paths = AppPaths(self.root / "lazy-cache-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
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
            actor=self.actor,
            lazy_pages=True,
        )
        window.show()
        self.app.processEvents()
        QTest.qWait(20)
        initial_index = next(
            index
            for index in range(window.nav_list.count())
            if window.nav_list.item(index).text() == "AI提交"
        )
        create_index = next(
            index
            for index in range(window.nav_list.count())
            if window.nav_list.item(index).text() == "普通创作"
        )
        with patch.object(window.new_task_page, "refresh") as refresh:
            window.nav_list.setCurrentRow(create_index)
            self.app.processEvents()
            window.nav_list.setCurrentRow(initial_index)
            self.app.processEvents()
            refresh.assert_not_called()
            window._mark_pages_dirty("AI提交")
            window.nav_list.setCurrentRow(create_index)
            window.nav_list.setCurrentRow(initial_index)
            self.app.processEvents()
            refresh.assert_called_once_with()
        window.close()
        window.deleteLater()

    def test_submission_page_uses_async_paged_repository_queries(
        self,
    ) -> None:
        write_task_images(self.root)
        project = make_project()
        task = make_task("approved")
        project_queries = []
        task_queries = []
        self.repository.list_board_projects_page = (
            lambda _actor, statuses, query, page: (
                project_queries.append((statuses, query, page))
                or PageResult([project], 125, page.page, page.page_size)
            )
        )
        self.repository.list_board_tasks_for_project_page = (
            lambda project_id, _actor, statuses, query, page: (
                task_queries.append(
                    (project_id, statuses, query, page)
                )
                or PageResult([task], 1, page.page, page.page_size)
            )
        )
        page = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "records",
            auto_load=False,
            async_mode=True,
        )
        page.refresh()
        for _ in range(100):
            self.app.processEvents()
            if page.task_table.rowCount():
                break
            QTest.qWait(10)
        self.assertEqual(page.project_table.rowCount(), 1)
        self.assertEqual(page.task_table.rowCount(), 1)
        self.assertEqual(project_queries[0][2].page_size, 100)
        self.assertEqual(task_queries[0][3].page_size, 100)
        self.assertIn("共 125 条", page.project_pagination.summary.text())
        page.project_pagination.next_button.click()
        for _ in range(100):
            self.app.processEvents()
            if len(project_queries) >= 2:
                break
            QTest.qWait(10)
        self.assertEqual(project_queries[-1][2].page, 2)
        page.deleteLater()

    def test_submission_page_ignores_stale_async_response(self) -> None:
        import time

        write_task_images(self.root)
        project = make_project()
        task = make_task("approved")
        calls = []

        def load_projects(_actor, _statuses, query, page):
            calls.append(query.keyword)
            if query.keyword == "旧":
                time.sleep(0.08)
                project_code = "旧任务"
            else:
                project_code = "新任务"
            return PageResult(
                [replace(project, project_code=project_code)],
                1,
                page.page,
                page.page_size,
            )

        self.repository.list_board_projects_page = load_projects
        self.repository.list_board_tasks_for_project_page = (
            lambda _project_id, _actor, _statuses, _query, page: PageResult(
                [task],
                1,
                page.page,
                page.page_size,
            )
        )
        page = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "records",
            auto_load=False,
            async_mode=True,
        )
        page.search_edit.setText("旧")
        page._request_project_page()
        page.search_edit.setText("新")
        page._request_project_page()
        for _ in range(100):
            self.app.processEvents()
            if page.project_table.rowCount() == 1:
                break
            QTest.qWait(10)
        QTest.qWait(120)
        self.app.processEvents()
        self.assertEqual(
            page.project_table.item(0, 0).text(),
            "新任务",
        )
        self.assertEqual(set(calls), {"旧", "新"})
        page.deleteLater()

    def test_ai_main_window_has_task_center_submenus(self) -> None:
        paths = AppPaths(self.root / "ai-window-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
        actor = AuthUser(
            4,
            "ai_amy",
            "AI制作Amy",
            "ai",
            menu_permissions=(
                "new_task",
                "assign_tasks",
                "ai_generate",
                "create",
                "batch",
                "history",
                "library",
            ),
        )
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
            [
                "AI提交",
                "待分配",
                "AI生图",
                "普通创作",
                "批量换装",
                "历史记录",
                "作品库",
            ],
        )
        self.assertEqual(
            window.nav_list._buttons[1].property("submenu"),
            True,
        )
        self.assertEqual(
            window.nav_list._buttons[2].property("submenu"),
            True,
        )
        self.assertEqual(
            window.nav_list._buttons[3].property("submenu"),
            True,
        )
        self.assertEqual(
            window.nav_list._buttons[4].property("submenu"),
            True,
        )
        window.close()
        window.deleteLater()

    def test_project_retry_requires_ai_generation_menu(self) -> None:
        paths = AppPaths(self.root / "retry-permission-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
        actor = AuthUser(
            4,
            "ai_amy",
            "AI制作Amy",
            "ai",
            menu_permissions=(
                "new_task",
                "history",
            ),
        )
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
        with patch(
            "yixin.ui.main_window.QMessageBox.warning"
        ) as warning:
            window._open_project_retry(11, 41)
        warning.assert_called_once()
        self.assertNotEqual(
            window.stack.currentWidget(),
            window.ai_generation_page,
        )
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
                "载入参数",
                "保存当前成品",
                "上传PS成品",
                "与原模特图对比",
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
        self.assertEqual(upload_manual.call_args.args[2].id, task.id)
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

    def test_pending_review_supports_multi_select_batch_actions(self) -> None:
        write_task_images(self.root)
        first = make_task("awaiting_acceptance")
        second = replace(
            first,
            id=12,
            task_code="春季项目-123-001-002",
        )
        self.repository.list_projects = lambda **_: [
            make_project("in_progress")
        ]
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            first,
            second,
        ]
        page = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "pending",
        )
        page.show()
        self.app.processEvents()
        self.assertEqual(
            page.task_table.selectionMode(),
            QAbstractItemView.SelectionMode.ExtendedSelection,
        )
        self.assertTrue(page.selection_hint.isVisible())
        page.task_table.selectRow(0)
        page.task_table.setCurrentCell(
            1,
            0,
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self.app.processEvents()
        self.assertEqual(len(page._selected_tasks()), 2)
        self.assertEqual(page.approve_button.text(), "验收通过（2）")
        self.assertEqual(page.return_button.text(), "退回返工（2）")
        with (
            patch(
                "yixin.ui.submission_list_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(page, "refresh"),
        ):
            page._approve_task()
        self.assertEqual(
            {request[0] for request in self.repository.review_requests},
            {11, 12},
        )
        self.assertTrue(
            all(request[2] for request in self.repository.review_requests)
        )
        self.repository.review_requests.clear()
        with (
            patch(
                "yixin.ui.submission_list_page.QInputDialog.getMultiLineText",
                return_value=("请统一调整领口", True),
            ),
            patch.object(page, "refresh"),
        ):
            page._return_task()
        self.assertEqual(
            {
                (request[0], request[2], request[3])
                for request in self.repository.review_requests
            },
            {
                (11, False, "请统一调整领口"),
                (12, False, "请统一调整领口"),
            },
        )
        page.deleteLater()

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
            [
                center.queue_table.horizontalHeaderItem(index).text()
                for index in range(center.queue_table.columnCount())
            ],
            [
                "主任务编码",
                "货号",
                "子任务编码",
                "轮次",
                "操作人员",
                "API 平台",
                "状态",
                "创建时间",
                "结果数",
            ],
        )
        self.assertEqual(center.queue_refresh_button.text(), "立即刷新")
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
                "裁剪当前图",
                "恢复原图",
                "移除所选",
                "清空素材",
            ],
        )
        self.assertFalse(center.skip_button.isEnabled())
        self.assertFalse(center.add_source_library_button.isEnabled())
        self.assertTrue(center.crop_source_button.isEnabled())
        self.assertTrue(center.restore_source_button.isEnabled())
        self.assertTrue(center.remove_source_button.isEnabled())
        self.assertTrue(center.clear_sources_button.isEnabled())
        center.source_strip.clearSelection()
        center.source_strip.setCurrentRow(-1)
        center._update_controls()
        self.assertFalse(center.crop_source_button.isEnabled())
        self.assertFalse(center.restore_source_button.isEnabled())
        self.assertFalse(center.remove_source_button.isEnabled())
        self.assertTrue(center.clear_sources_button.isEnabled())
        center.source_strip.setCurrentRow(0)
        center._update_controls()
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
        self.assertEqual(center.gallery.selected_result_ids(), [21])
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
        self.assertTrue(center.restore_source_button.isEnabled())
        self.assertFalse(center.clear_sources_button.isEnabled())
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
        working = make_task("awaiting_acceptance")
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

    def test_task_center_queue_shows_all_subtasks_in_main_task(self) -> None:
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
        self.repository.list_board_tasks = lambda *_args, **_kwargs: [
            first,
            second,
        ]
        self.repository.project_attempts = [
            {
                "id": 71,
                "task_id": first.id,
                "attempt_no": 2,
                "operator_id": self.actor.id,
                "operator_name": self.actor.display_name,
                "project_code": first.project_code,
                "product_code": first.product_code,
                "access_project_name": first.access_project_name,
                "task_code": first.task_code,
                "model": "gemini-3.1-flash-image",
                "status": ATTEMPT_QUEUED,
                "created_at": datetime.now(),
                "generated_image_count": 0,
                "result_count": 0,
            },
            {
                "id": 72,
                "task_id": second.id,
                "attempt_no": 1,
                "operator_id": self.actor.id,
                "operator_name": self.actor.display_name,
                "project_code": second.project_code,
                "product_code": second.product_code,
                "access_project_name": second.access_project_name,
                "task_code": second.task_code,
                "model": "gemini-3.1-flash-image",
                "status": ATTEMPT_SUCCEEDED,
                "created_at": datetime.now(),
                "generated_image_count": 1,
                "result_count": 1,
            },
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
            FileStore(AppPaths(self.root / "queue-all-data")),
            AppSettings(Database(self.root / "queue-all.db")),
        )
        center.resize(1480, 900)
        center.show()
        self.app.processEvents()
        center._refresh_queue()
        self.assertEqual(center.queue_table.rowCount(), 2)
        self.assertEqual(center.queue_table.item(0, 2).text(), first.task_code)
        self.assertEqual(center.queue_table.item(1, 2).text(), second.task_code)
        center.task_table.selectRow(1)
        self.app.processEvents()
        center._refresh_queue()
        self.assertEqual(center.queue_table.rowCount(), 2)
        calls = []
        original = self.repository.list_project_attempts
        self.repository.list_project_attempts = (
            lambda project_id: (
                calls.append(project_id)
                or original(project_id)
            )
        )
        center.queue_refresh_button.click()
        self.assertEqual(calls, [first.project_id])
        center.deleteLater()

    def test_task_center_skip_remaining_marks_project_tasks(self) -> None:
        write_task_images(self.root)
        working = make_task("awaiting_acceptance")
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
        ]
        self.assertEqual(button_names[-1], "提交验收")
        self.assertEqual(center.skip_button.text(), "结束剩余子任务")
        self.assertTrue(center.skip_button.isEnabled())
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
                "access_project_name": "春季项目",
                "operator_name": "AI制作Amy",
                "model": "gemini-3.1-flash-image",
                "status": "succeeded",
                "prompt_text": "项目历史提示词",
                "created_at": datetime.now(),
                "result_count": 1,
            },
            {
                "id": 41,
                "task_id": task.id,
                "attempt_no": 2,
                "project_code": "春季项目-123-002",
                "task_code": "春季项目-123-002-001",
                "product_code": "123",
                "project_name": "123",
                "access_project_name": "春季项目",
                "operator_name": "AI制作Amy",
                "model": "gemini-3.1-flash-image",
                "status": "succeeded",
                "prompt_text": "同名项目",
                "created_at": datetime.now(),
                "result_count": 0,
            },
            {
                "id": 42,
                "task_id": task.id,
                "attempt_no": 3,
                "project_code": "刘一一-CD1A0954开衫-001",
                "task_code": "刘一一-CD1A0954开衫-001-001",
                "product_code": "CD1A0954开衫",
                "project_name": "CD1A0954开衫",
                "access_project_name": "刘一一",
                "operator_name": "AI制作Amy",
                "model": "gemini-3.1-flash-image",
                "status": "succeeded",
                "prompt_text": "外部项目",
                "created_at": datetime.now(),
                "result_count": 0,
            },
            {
                "id": 43,
                "task_id": task.id,
                "attempt_no": 4,
                "project_code": "旧项目编码",
                "task_code": "旧项目编码-001",
                "product_code": "旧货号",
                "project_name": "旧货号",
                "operator_name": "AI制作Amy",
                "model": "gemini-3.1-flash-image",
                "status": "succeeded",
                "prompt_text": "旧数据",
                "created_at": datetime.now(),
                "result_count": 0,
            },
        ]
        page = ProjectHistoryPage(
            self.repository,
            self.resolver,
            self.actor,
        )
        self.assertEqual(page.table.rowCount(), 4)
        self.assertEqual(
            {
                page.project_filter.itemText(index)
                for index in range(page.project_filter.count())
            },
            {"全部项目", "春季项目", "刘一一", "旧货号"},
        )
        spring_index = page.project_filter.findData("春季项目")
        page.project_filter.setCurrentIndex(spring_index)
        self.assertEqual(page.table.rowCount(), 2)
        page.project_filter.setCurrentIndex(0)
        self.assertEqual(page.table.rowCount(), 4)
        target_row = next(
            row
            for row in range(page.table.rowCount())
            if page.table.item(row, 0).data(
                Qt.ItemDataRole.UserRole
            )
            == 40
        )
        self.assertEqual(
            page.table.item(target_row, 1).text(),
            task.task_code,
        )
        page.table.selectRow(target_row)
        self.app.processEvents()
        self.assertEqual(page.gallery.list_widget.count(), 1)
        self.assertEqual(
            page.gallery.preview.current_path,
            result_path.resolve(),
        )
        page.product_search.setText("不存在")
        self.assertEqual(page.table.rowCount(), 0)
        page.product_search.clear()
        self.assertEqual(page.table.rowCount(), 4)
        self.assertTrue(page.table.isSortingEnabled())
        page.deleteLater()
        database = Database(self.root / "history-tabs.db")
        database.initialize()
        paths = AppPaths(self.root / "history-tabs-data")
        paths.ensure()
        client = NanoBananaClient(lambda: "test-key")
        history = UnifiedHistoryPage(
            database,
            LibraryService(database, FileStore(paths), paths),
            TaskWorker(database, client, FileStore(paths)),
            self.repository,
            self.resolver,
            self.actor,
        )
        self.assertFalse(hasattr(history, "tabs"))
        history.deleteLater()

    def test_unified_history_merges_project_and_ordinary_records(self) -> None:
        write_task_images(self.root)
        task = make_task("approved")
        product_root = self.root / task.project_relative_root
        result_path = product_root / "AI成品" / "result.png"
        result_path.parent.mkdir(parents=True)
        Image.new("RGB", (30, 40), "orange").save(result_path)
        thumbnail_path = product_root / "AI成品" / "thumb_result.png"
        Image.new("RGB", (10, 10), "orange").save(thumbnail_path)
        result = GenerationResult(
            id=30,
            attempt_id=40,
            result_index=0,
            relative_path=result_path.relative_to(product_root).as_posix(),
            thumbnail_relative_path=thumbnail_path.relative_to(
                product_root
            ).as_posix(),
            content_hash="b" * 64,
            mime="image/png",
            width=30,
            height=40,
            is_final=True,
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
                "access_project_name": "春季项目",
                "operator_name": "AI制作Amy",
                "model": "gemini-3.1-flash-image",
                "provider_name": "A6API",
                "requested_provider_code": "a6api",
                "requested_provider_name": "A6API",
                "status": "succeeded",
                "prompt_text": "项目历史提示词",
                "aspect_ratio": "3:4",
                "resolution": "2K",
                "n": 1,
                "created_at": datetime.now(),
                "result_count": 1,
            }
        ]
        self.repository.list_attempts = lambda _task_id: [
            GenerationAttempt(
                id=40,
                task_id=task.id,
                attempt_no=1,
                operator_id=4,
                model="gemini-3.1-flash-image",
                prompt_text="项目历史提示词",
                aspect_ratio="3:4",
                resolution="2K",
                n=1,
                status=ATTEMPT_SUCCEEDED,
                source_revision=1,
                unit_price=Decimal("1.00"),
                generated_image_count=1,
                provider_code="a6api",
                provider_name="A6API",
                requested_provider_code="a6api",
                requested_provider_name="A6API",
                created_at=datetime.now(),
            )
        ]
        paths = AppPaths(self.root / "unified-history-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        store = FileStore(paths)
        database.create_task(
            ImageRequest(
                model="gemini-3.1-flash-image",
                prompt="普通创作历史",
                n=1,
                aspect_ratio="3:4",
                resolution="2K",
                mode="text",
            )
        )
        claimed = database.claim_next_queued()
        database.transition_task(int(claimed["id"]), TASK_SUCCESS)
        library = LibraryService(database, store, paths)
        client = NanoBananaClient(lambda: "test-key")
        page = UnifiedHistoryPage(
            database,
            library,
            TaskWorker(database, client, store),
            self.repository,
            self.resolver,
            self.actor,
        )
        self.assertFalse(hasattr(page, "tabs"))
        self.assertEqual(page.table.rowCount(), 2)
        self.assertEqual(
            {
                page.table.item(row, 0).text()
                for row in range(page.table.rowCount())
            },
            {"项目", "普通"},
        )
        page.type_filter.setCurrentIndex(
            page.type_filter.findData("ordinary")
        )
        self.assertEqual(page.table.rowCount(), 1)
        self.assertEqual(page.table.item(0, 0).text(), "普通")
        page.type_filter.setCurrentIndex(0)
        page.status_filter.setCurrentIndex(
            page.status_filter.findData(ATTEMPT_SUCCEEDED)
        )
        self.assertEqual(page.table.rowCount(), 2)
        page.status_filter.setCurrentIndex(0)
        page.project_filter.setCurrentIndex(
            page.project_filter.findData("春季项目")
        )
        self.assertEqual(page.table.rowCount(), 1)
        self.assertEqual(page.table.item(0, 0).text(), "项目")
        self.assertEqual(
            page.dual_preview.result_pane.list_widget.count(),
            1,
        )
        self.assertEqual(
            page.dual_preview.reference_pane.list_widget.count(),
            3,
        )
        self.assertEqual(
            page.dual_preview.result_pane.list_widget.currentRow(),
            0,
        )
        self.assertEqual(
            page.dual_preview.reference_pane.list_widget.currentRow(),
            2,
        )
        self.assertEqual(
            page.dual_preview.reference_pane.list_widget.item(2).text(),
            "模特",
        )
        self.assertEqual(
            page.preview.current_path,
            result_path.resolve(),
        )
        self.assertTrue(page.save_local_button.isEnabled())
        reference_strip = page.dual_preview.reference_pane.list_widget
        reference_strip.itemClicked.emit(reference_strip.item(2))
        self.app.processEvents()
        self.assertFalse(page.save_local_button.isEnabled())
        result_strip = page.dual_preview.result_pane.list_widget
        result_strip.itemClicked.emit(result_strip.item(0))
        self.app.processEvents()
        self.assertTrue(page.save_local_button.isEnabled())
        emitted: list[tuple[int, int]] = []
        page.project_retry_requested.connect(
            lambda task_id, attempt_id: emitted.append(
                (task_id, attempt_id)
            )
        )
        page._retry_current()
        self.assertEqual(emitted, [(task.id, 40)])
        with patch(
            "yixin.ui.unified_history_page.show_saved_path"
        ):
            page._save_library()
        self.assertEqual(len(database.list_works()), 1)
        with patch(
            "yixin.ui.image_preview.LargeImageDialog"
        ) as dialog_cls:
            page.dual_preview.result_pane._open_zoom()
        dialog_cls.return_value.exec.assert_called_once()
        page.deleteLater()

    def test_unified_history_cancels_only_queued_project_attempt(self) -> None:
        task = make_task("claimed")
        self.repository.tasks_by_id[task.id] = task
        self.repository.project_attempt_history = [
            {
                "id": 50,
                "task_id": task.id,
                "attempt_no": 1,
                "project_code": task.project_code,
                "task_code": task.task_code,
                "product_code": task.product_code,
                "access_project_name": "春季项目",
                "operator_name": "AI制作Amy",
                "model": "gemini-3.1-flash-image",
                "status": ATTEMPT_QUEUED,
                "prompt_text": "排队任务",
                "created_at": datetime.now(),
                "result_count": 0,
            }
        ]
        cancelled: list[int] = []
        self.repository.cancel_queued_generation_attempt = (
            lambda attempt_id, _actor: cancelled.append(attempt_id)
        )
        paths = AppPaths(self.root / "history-cancel-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        store = FileStore(paths)
        client = NanoBananaClient(lambda: "test-key")
        page = UnifiedHistoryPage(
            database,
            LibraryService(database, store, paths),
            TaskWorker(database, client, store),
            self.repository,
            self.resolver,
            self.actor,
        )
        emitted: list[int] = []
        page.project_attempt_cancelled.connect(emitted.append)
        self.assertTrue(page.cancel_button.isEnabled())
        page._cancel_current()
        self.assertEqual(cancelled, [50])
        self.assertEqual(emitted, [50])
        page.deleteLater()

    def test_unified_history_async_page_and_detail_loading(self) -> None:
        write_task_images(self.root)
        task = make_task("approved")
        product_root = self.root / task.project_relative_root
        result_path = product_root / "AI成品" / "async-result.png"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 50), "orange").save(result_path)
        result = GenerationResult(
            id=70,
            attempt_id=80,
            result_index=0,
            relative_path=result_path.relative_to(product_root).as_posix(),
            thumbnail_relative_path=result_path.relative_to(
                product_root
            ).as_posix(),
            content_hash="d" * 64,
            mime="image/png",
            width=40,
            height=50,
            is_final=True,
        )
        row = {
            "id": 80,
            "task_id": task.id,
            "assignee_id": task.assignee_id,
            "attempt_no": 1,
            "project_code": task.project_code,
            "task_code": task.task_code,
            "product_code": task.product_code,
            "project_name": task.project_name,
            "access_project_name": "春季项目",
            "operator_name": "AI制作Amy",
            "model": task.model,
            "provider_name": "A6API",
            "status": ATTEMPT_SUCCEEDED,
            "prompt_text": "异步历史详情",
            "aspect_ratio": "3:4",
            "resolution": "2K",
            "n": 1,
            "created_at": datetime.now(),
            "result_count": 1,
        }
        self.repository.tasks_by_id[task.id] = task
        self.repository.attempt_results[80] = [result]
        self.repository.list_project_attempt_history_page = (
            lambda _actor, _query, page: PageResult(
                [row],
                1,
                page.page,
                page.page_size,
            )
        )
        self.repository.list_access_project_names = (
            lambda _actor: ["春季项目"]
        )
        paths = AppPaths(self.root / "async-history-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        database.list_tasks_page = (
            lambda _query, page: PageResult(
                [],
                0,
                page.page,
                page.page_size,
            )
        )
        store = FileStore(paths)
        client = NanoBananaClient(lambda: "test-key")
        page = UnifiedHistoryPage(
            database,
            LibraryService(database, store, paths),
            TaskWorker(database, client, store),
            self.repository,
            self.resolver,
            self.actor,
            auto_load=False,
            async_mode=True,
        )
        page.refresh()
        loaded = False
        for _ in range(150):
            self.app.processEvents()
            loaded = (
                page.table.rowCount() == 1
                and page.dual_preview.result_pane.list_widget.count() == 1
            )
            if loaded:
                break
            QTest.qWait(10)
        self.assertTrue(loaded)
        self.assertIn("共 1 条", page.pagination.summary.text())
        self.assertEqual(
            page.project_filter.findData("春季项目") >= 0,
            True,
        )
        page.deleteLater()

    def test_large_image_dialog_supports_pan_and_zoom(self) -> None:
        image_path = self.root / "zoom-history.png"
        Image.new("RGB", (320, 240), "purple").save(image_path)
        dialog = LargeImageDialog(image_path)
        self.assertEqual(
            dialog.view.dragMode(),
            QGraphicsView.DragMode.ScrollHandDrag,
        )
        dialog._set_scale(0.01)
        self.assertEqual(dialog._scale, 0.1)
        dialog._set_scale(8)
        self.assertEqual(dialog._scale, 4.0)
        dialog._fit_to_window()
        self.assertGreater(dialog._scale, 0)
        dialog._release_resources()
        self.assertIsNone(dialog.view.scene())
        self.assertIsNone(dialog.pixmap_item)
        self.assertTrue(dialog._pixmap.isNull())
        dialog.close()
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
