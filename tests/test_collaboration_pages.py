import importlib.util
import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.qt_test_support import flush_qt_deletes

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None

if HAS_PYSIDE:
    from PIL import Image
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QGraphicsView,
        QLabel,
        QMessageBox,
        QPushButton,
    )

    from yixin.api import NanoBananaClient
    from yixin.collaboration.folders import current_date_folder
    from yixin.collaboration.reports import aggregate_report
    from yixin.collaboration.reports import ReportTaskRow
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
        OperationAccountCandidate,
        OperationAccountSyncResult,
        ProjectAssetInput,
        ProjectRecord,
        ProjectTask,
    )
    from yixin.constants import (
        PROTOCOL_OPENAI_CHAT_IMAGE,
        PROTOCOL_OPENAI_IMAGES,
        TASK_SUCCESS,
    )
    from yixin.database import Database
    from yixin.library import LibraryService
    from yixin.models import ImageRequest
    from yixin.paging import PageResult
    from yixin.paths import AppPaths
    from yixin.security import SecretStore
    from yixin.storage import FileStore
    from yixin.ui.admin_page import (
        AddUserDialog,
        AdminPage,
        ApiProviderDialog,
        EditUserDialog,
    )
    from yixin.ui.collaboration_login import LoginDialog
    from yixin.ui.components import FilterBar
    from yixin.ui.dialogs import SettingsDialog
    from yixin.ui.main_window import MainWindow, TopNavigation
    from yixin.ui.reports_page import ReportsPage
    from yixin.ui.submission_list_page import SubmissionListPage
    from yixin.ui.unified_history_page import (
        UnifiedHistoryPage,
    )
    from yixin.ui.image_preview import (
        DualImagePreview,
        ImageZoomView,
        ImagePreviewPane,
    )
    from yixin.ui.task_assignment_page import TaskAssignmentPage
    from yixin.ui.ai_generation_page import AIGenerationPage
    from yixin.ui.task_create_page import (
        CheckableHeaderView,
        TaskCreatePage,
    )
    from yixin.ui.theme import APP_STYLESHEET
    from yixin.worker import TaskWorker
    from tests.helpers import png_bytes




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
        self.report_result = None

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

    def list_exception_projects(self, _cutoff, **_kwargs):
        return self.list_projects()

    def list_exception_tasks(self, project_id, _cutoff):
        return self.list_project_tasks(project_id)

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

    def list_assignment_project_counts(self, _actor):
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

    def create_user(self, actor, *args, **kwargs):
        self.user_updates.append((actor, None, {"create": args, **kwargs}))
        return AuthUser(
            999,
            args[0],
            args[1],
            args[2],
        )

    def set_user_active(self, actor, user_id, active):
        self.user_updates.append(
            (actor, user_id, {"active": active})
        )

    def reset_password(self, actor, user_id, password):
        self.user_updates.append(
            (actor, user_id, {"password": password})
        )

    def delete_user(self, actor, user_id):
        self.user_updates.append(
            (actor, user_id, {"delete": True})
        )

    def preview_operation_accounts(self, _actor):
        return (
            OperationAccountCandidate(
                "YY01",
                "春季项目",
                (("internal", "春季项目"),),
            ),
        )

    def sync_operation_accounts_from_shared(self, _actor):
        return OperationAccountSyncResult(("YY01",), (), ())

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

    def list_report_project_options(self, _actor):
        return [("internal", "春季项目")]

    def report(self, _actor, _report_filter):
        return self.report_result

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
    first_garment_hash = hashlib.sha256(
        png_bytes((255, 0, 0), (20, 20))
    ).hexdigest()
    second_garment_hash = hashlib.sha256(
        png_bytes((0, 128, 0), (20, 20))
    ).hexdigest()
    model_hash = hashlib.sha256(
        png_bytes((0, 0, 255), (20, 20))
    ).hexdigest()
    garments = (
        ProjectAssetInput(
            "garment",
            "衣服图/衣服1.png",
            "衣服1.png",
            first_garment_hash,
            "image/png",
            20,
            20,
        ),
        ProjectAssetInput(
            "garment",
            "衣服图/衣服2.png",
            "衣服2.png",
            second_garment_hash,
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
        garment_hash=first_garment_hash,
        model_asset_id=1,
        model_path="模特图/模特1.png",
        model_name="模特1.png",
        model_hash=model_hash,
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
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = FakeCentralRepository()
        self.actor = AuthUser(1, "admin", "春季项目", ROLE_ADMIN)
        self.resolver = SharedPathResolver(self.root)

    def tearDown(self) -> None:
        flush_qt_deletes(self.app)
        self.temp.cleanup()

    @classmethod
    def tearDownClass(cls) -> None:
        flush_qt_deletes(cls.app)

    def test_new_pages_build_with_empty_database(self) -> None:
        create = TaskCreatePage(
            self.repository,
            self.resolver,
            self.actor,
        )
        records = SubmissionListPage(
            self.repository,
            self.resolver,
            self.actor,
            "records",
        )
        admin = AdminPage(self.repository, self.actor)
        title = create.findChild(QLabel, "PageTitle")
        self.assertIsNotNone(title)
        self.assertEqual(title.text(), "任务提交")
        self.assertEqual(create.product_table.rowCount(), 0)
        self.assertEqual(records.project_table.rowCount(), 0)
        self.assertEqual(admin.user_table.rowCount(), 0)
        for page in (create, records, admin):
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
        self.assertFalse(page.result_panel.isHidden())
        self.assertIs(
            page.result_panel,
            page.dual_preview.result_pane,
        )
        self.assertIs(
            page.reference_panel,
            page.dual_preview.reference_pane,
        )
        self.assertFalse(hasattr(page, "urgent_button"))
        self.assertFalse(hasattr(page, "review_actions"))
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
        self.repository.list_assignment_project_counts = (
            lambda _actor: [("春季项目", 1)]
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
        self.assertEqual(
            page.project_filter.currentText(),
            "全部项目（1）",
        )
        self.assertGreaterEqual(
            page.project_filter.findData("春季项目"),
            0,
        )
        self.assertFalse(hasattr(page, "task_pagination"))
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
        self.assertTrue(page.count_spin.isHidden())
        original_hashes = tuple(
            image.content_hash for image in page.garment_assets
        )
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            page._clear_role("garment")
        self.assertEqual(page.garment_assets, [])
        self.assertTrue(page.restore_all_button.isEnabled())
        page.restore_all_button.click()
        self.assertEqual(len(page.garment_assets), 2)
        self.assertEqual(
            tuple(image.content_hash for image in page.garment_assets),
            original_hashes,
        )
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
        self.assertEqual(page.count_spin.value(), 1)
        self.assertTrue(page.count_spin.isHidden())
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

    def test_top_navigation_uses_flat_single_row_menu(self) -> None:
        labels = [
            "任务提交",
            "提交记录",
            "待分配",
            "AI生图",
            "普通创作",
            "批量换装",
            "历史记录",
            "作品库",
            "经营报告",
            "管理控制台",
        ]
        nav = TopNavigation(
            labels,
            display_name="这是一个很长很长的管理员账号名称",
            role_label="管理员",
            settings_visible=True,
        )
        nav.resize(1180, 60)
        nav.setStyleSheet(APP_STYLESHEET)
        nav.show()
        self.app.processEvents()
        self.assertEqual(
            [button.text() for button in nav._buttons],
            labels,
        )
        self.assertFalse(hasattr(nav, "secondary_nav"))
        self.assertLessEqual(nav.sizeHint().width(), 1180)
        self.assertTrue(all(button.isVisible() for button in nav._buttons))
        nav.setCurrentRow(0)
        self.assertTrue(nav._buttons[0].isChecked())
        self.assertFalse(any(button.isChecked() for button in nav._buttons[1:]))
        nav.close()
        nav.deleteLater()

    def test_filter_bar_wraps_without_squeezing_controls(self) -> None:
        filter_bar = FilterBar()
        buttons = [QPushButton(f"筛选 {index}") for index in range(8)]
        for button in buttons:
            button.setFixedSize(100, 32)
            filter_bar.layout.addWidget(button)
        filter_bar.resize(380, 120)
        filter_bar.show()
        self.app.processEvents()
        self.assertGreater(
            len({button.geometry().y() for button in buttons}),
            1,
        )
        self.assertTrue(
            all(button.width() >= button.minimumSizeHint().width()
                for button in buttons)
        )
        filter_bar.resize(1200, 80)
        self.app.processEvents()
        self.assertEqual(
            len({button.geometry().y() for button in buttons}),
            1,
        )
        filter_bar.close()
        filter_bar.deleteLater()

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

    def test_admin_can_delete_selected_account(self) -> None:
        ai_user = AuthUser(
            8,
            "ai_amy",
            "AI制作Amy",
            "ai",
        )
        self.repository.list_users = lambda role=None: [ai_user]
        page = AdminPage(self.repository, self.actor)
        page.user_table.selectRow(0)
        self.assertEqual(
            page.delete_user_button.objectName(),
            "DangerButton",
        )
        with (
            patch(
                "yixin.ui.admin_page.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(page, "_refresh_users"),
        ):
            page._delete_user()
        self.assertEqual(
            self.repository.user_updates[-1],
            (self.actor, 8, {"delete": True}),
        )
        page.deleteLater()

    def test_admin_can_sync_operation_accounts_from_shared(self) -> None:
        page = AdminPage(self.repository, self.actor)
        with (
            patch(
                "yixin.ui.admin_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch(
                "yixin.ui.admin_page.QMessageBox.information"
            ) as information,
            patch.object(page, "_refresh_users"),
        ):
            page._sync_operation_accounts()
        information.assert_called_once()
        self.assertIn(
            "新建 1 个",
            information.call_args.args[2],
        )
        page.deleteLater()

    def test_reports_page_uses_valid_generation_metrics(self) -> None:
        now = datetime.now()
        row = ReportTaskRow(
            project_id=1,
            project_code="P001",
            project_scope="internal",
            project_name="春季项目",
            product_code="G001",
            project_status="completed",
            priority="normal",
            task_id=1,
            task_code="P001-001",
            task_status="approved",
            assignee_id=8,
            owner_name="运营A",
            assignee_name="制作A",
            model="model-a",
            garment_name="衣服",
            model_name="模特",
            final_quantity=2,
            generated_image_count=4,
            attempt_count=2,
            successful_attempt_count=1,
            failed_attempt_count=1,
            generation_cost=Decimal("4.00"),
            project_created_at=now - timedelta(hours=3),
            claimed_at=now - timedelta(hours=2),
            work_started_at=now - timedelta(hours=2),
            submitted_at=now,
            approved_at=now,
            provider_name="A6API",
        )
        report = aggregate_report([row])
        self.repository.report_result = report
        page = ReportsPage(
            self.repository,
            self.actor,
            auto_load=False,
        )
        page._render_report(report)
        self.assertEqual(
            page.metric_cards["final_images"].value_label.text(),
            "2 张",
        )
        self.assertEqual(
            page.metric_cards["generation_success"].value_label.text(),
            "50.0%",
        )
        self.assertEqual(
            page.metric_cards["failed_attempts"].value_label.text(),
            "1 次",
        )
        all_text = " ".join(
            label.text()
            for label in page.findChildren(QLabel)
            if label.text()
        )
        self.assertNotIn("返工", all_text)
        self.assertNotIn("PS成品", all_text)
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
            ["任务提交", "提交记录"],
        )
        with patch.object(window.new_task_page, "refresh") as refresh:
            window.show()
            self.app.processEvents()
            QTest.qWait(20)
            refresh.assert_called_once_with()
        window.close()
        window.deleteLater()

    def test_empty_menu_permissions_stay_empty(self) -> None:
        paths = AppPaths(self.root / "empty-menu-data")
        paths.ensure()
        database = Database(paths.database)
        database.initialize()
        file_store = FileStore(paths)
        client = NanoBananaClient(lambda: None)
        actor = AuthUser(
            3,
            "operator",
            "运营小林",
            ROLE_OPERATIONS,
            menu_permissions=(),
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
            lazy_pages=True,
        )
        labels = [
            window.nav_list.item(index).text()
            for index in range(window.nav_list.count())
        ]
        self.assertEqual(labels, ["无可用菜单"])
        window.close()
        window.deleteLater()

    def test_ai_user_can_submit_tasks(self) -> None:
        page = TaskCreatePage(
            self.repository,
            self.resolver,
            AuthUser(
                4,
                "ai_amy",
                "AI制作Amy",
                "ai",
            ),
            auto_load=False,
        )
        self.assertTrue(page.create_button.isEnabled())
        page.deleteLater()

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
            if window.nav_list.item(index).text() == "任务提交"
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
            window._mark_pages_dirty("任务提交")
            window.nav_list.setCurrentRow(create_index)
            window.nav_list.setCurrentRow(initial_index)
            self.app.processEvents()
            refresh.assert_called_once_with()
        window.close()
        window.deleteLater()

    def test_admin_main_window_opens_reports_as_first_level_page(
        self,
    ) -> None:
        paths = AppPaths(self.root / "reports-window-data")
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
        labels = [
            window.nav_list.item(index).text()
            for index in range(window.nav_list.count())
        ]
        self.assertIn("经营报告", labels)
        report_index = labels.index("经营报告")
        window.nav_list.setCurrentRow(report_index)
        self.app.processEvents()
        self.assertTrue(hasattr(window, "reports_page"))
        self.assertIs(window.stack.currentWidget(), window.reports_page)
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
        self.assertEqual(task_queries[0][3].page, 1)
        self.assertEqual(task_queries[0][3].page_size, 100)
        self.assertIn("共 125 条", page.project_pagination.summary.text())
        self.assertFalse(hasattr(page, "task_pagination"))
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

    def test_ai_main_window_uses_flat_navigation(self) -> None:
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
                "任务提交",
                "待分配",
                "AI生图",
                "普通创作",
                "批量换装",
                "历史记录",
                "作品库",
            ],
        )
        for button in window.nav_list._buttons:
            self.assertFalse(button.property("submenu"))
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
        self.assertEqual(page.table.rowCount(), 2)
        self.assertIn(
            "项目",
            {
                page.table.item(row, 0).text()
                for row in range(page.table.rowCount())
            },
        )
        page.table.selectRow(
            next(
                row
                for row in range(page.table.rowCount())
                if page.table.item(row, 0).text() == "项目"
            )
        )
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
        result_pane = page.dual_preview.result_pane
        reference_pane = page.dual_preview.reference_pane
        self.assertIsInstance(
            result_pane.preview,
            ImageZoomView,
        )
        self.assertFalse(hasattr(result_pane, "_zoom_dialog"))
        reference_strip.setCurrentRow(0)
        reference_strip.setCurrentRow(1)
        self.app.processEvents()
        self.assertEqual(
            reference_pane.preview.current_path,
            reference_pane.items[1][1],
        )
        manual_record = page._project_record(
            {
                "id": 99,
                "task_id": task.id,
                "status": "succeeded",
                "created_at": datetime.now(),
                "project_code": task.project_code,
                "task_code": task.task_code,
                "product_code": task.product_code,
                "project_name": task.project_name,
                "access_project_name": "春季项目",
                "operator_name": "AI制作Amy",
                "model": "gemini-3.1-flash-image",
                "provider_name": "A6API",
                "prompt_text": "PS",
                "result_count": 1,
                "manual_result_count": 1,
            }
        )
        self.assertEqual(manual_record.status, "manual_ps")
        self.assertEqual(manual_record.status_label, "PS成品")
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

    def test_image_zoom_view_supports_pan_and_zoom(self) -> None:
        first_path = self.root / "compare-left.png"
        second_path = self.root / "compare-right.png"
        Image.new("RGB", (320, 240), "purple").save(first_path)
        Image.new("RGB", (280, 320), "orange").save(second_path)
        view = ImageZoomView()
        view.set_image(first_path, "单图")
        self.assertEqual(
            view.view.dragMode(),
            QGraphicsView.DragMode.ScrollHandDrag,
        )
        self.assertEqual(view.current_path, first_path)
        self.assertFalse(hasattr(view, "swap_button"))
        self.assertFalse(hasattr(view, "exit_compare_button"))
        view.set_zoom(0.01)
        self.assertEqual(view._scale, 0.1)
        view.set_zoom(8)
        self.assertEqual(view._scale, 4.0)
        view.fit_to_window()
        self.assertGreater(view._scale, 0)
        view.set_image(second_path, "替换图")
        self.assertEqual(view.current_path, second_path)
        view.deleteLater()

    def test_image_preview_always_replaces_original_image(self) -> None:
        paths = []
        for index, color in enumerate(("red", "green", "blue"), start=1):
            path = self.root / f"pair-{index}.png"
            Image.new("RGB", (80, 100), color).save(path)
            paths.append(path)
        pane = ImagePreviewPane("result", "结果图")
        pane.set_images(
            [
                (path, f"结果 {index}", index)
                for index, path in enumerate(paths, start=1)
            ],
            default_row=0,
        )
        self.assertEqual(pane.preview.current_path, paths[0])
        pane.list_widget.itemClicked.emit(pane.list_widget.item(1))
        self.assertEqual(pane.preview.current_path, paths[1])
        pane.list_widget.itemClicked.emit(pane.list_widget.item(2))
        self.assertEqual(pane.preview.current_path, paths[2])
        pane.deleteLater()

    def test_dual_preview_shared_toolbar_controls_active_side(self) -> None:
        root = self.root / "dual-toolbar"
        root.mkdir()
        results = []
        references = []
        for index, color in enumerate(("red", "orange"), start=1):
            path = root / f"result-{index}.png"
            Image.new("RGB", (100, 140), color).save(path)
            results.append((path, f"结果 {index}", index))
        for index, color in enumerate(("blue", "green"), start=1):
            path = root / f"reference-{index}.png"
            Image.new("RGB", (100, 140), color).save(path)
            references.append(
                (path, "模特" if index == 2 else f"参考 {index}", index)
            )
        widget = DualImagePreview()
        widget.set_assets(
            results,
            references,
            default_reference_row=1,
        )
        QTest.qWait(30)
        self.app.processEvents()
        self.assertEqual(widget.active_role, "result")
        self.assertEqual(
            widget.result_pane.preview.current_path,
            results[0][0],
        )
        self.assertEqual(
            widget.reference_pane.preview.current_path,
            references[1][0],
        )

        widget.result_pane.preview.set_zoom(2.0)
        result_scale = widget.result_pane.preview.scale
        self.assertGreater(result_scale, 1.0)
        widget.reference_pane.list_widget.itemClicked.emit(
            widget.reference_pane.list_widget.item(0)
        )
        QTest.qWait(30)
        self.app.processEvents()
        self.assertEqual(widget.active_role, "reference")
        self.assertEqual(widget.active_label.text(), "当前：参考图")
        reference_before = widget.reference_pane.preview.scale
        widget.zoom_in_button.click()
        self.app.processEvents()
        self.assertGreater(
            widget.reference_pane.preview.scale,
            reference_before,
        )
        self.assertEqual(
            widget.result_pane.preview.scale,
            result_scale,
        )

        reference_path = widget.reference_pane.preview.current_path
        widget.result_pane.list_widget.itemClicked.emit(
            widget.result_pane.list_widget.item(1)
        )
        self.assertEqual(widget.active_role, "result")
        self.assertEqual(
            widget.result_pane.preview.current_path,
            results[1][0],
        )
        self.assertEqual(
            widget.reference_pane.preview.current_path,
            reference_path,
        )

        empty = DualImagePreview()
        empty.set_assets(results, [])
        empty.set_active_role("reference")
        self.assertEqual(empty.active_role, "result")
        widget.deleteLater()
        empty.deleteLater()


if __name__ == "__main__":
    unittest.main()
