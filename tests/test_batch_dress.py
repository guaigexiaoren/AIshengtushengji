from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QPushButton,
)

from tests.helpers import make_image
from yixin.constants import (
    BUILTIN_PROMPT_TEMPLATES,
    MODE_DRESS_UP,
    MODE_FREE_COMPOSE,
    TASK_SUCCESS,
)
from yixin.collaboration.shared_paths import SharedPathResolver
from yixin.database import Database
from yixin.library import LibraryService
from yixin.models import ImageRequest
from yixin.paths import AppPaths
from yixin.security import SecretStore
from yixin.settings import INPUT_BATCH_GARMENT, INPUT_BATCH_PERSON, AppSettings
from yixin.storage import FileStore
from yixin.ui.batch_dress_page import (
    DRESS_UP_MIME,
    FreeComposeDialog,
    DressTaskGroup,
    PAIR_CARD_GAP,
    PAIR_CARD_MIN_WIDTH,
    scan_image_inputs,
)
from yixin.ui.main_window import MainWindow
from yixin.ui.theme import APP_STYLESHEET
from yixin.ui.widgets import natural_sort_key
from yixin.worker import TaskWorker
from tests.collaboration_fakes import EmptyCentralRepository, fake_ai_user


class BatchDressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(APP_STYLESHEET)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp.name) / "批量 数据")
        self.paths.ensure()
        self.database = Database(self.paths.database)
        self.database.initialize()
        self.store = FileStore(self.paths)
        self.secret_store = SecretStore(self.paths.api_key_file)
        self.secret_store.save("sk-batch-test-12345")
        self.worker = TaskWorker(self.database, object(), self.store)
        self.library = LibraryService(self.database, self.store, self.paths)
        self.window = MainWindow(
            paths=self.paths,
            database=self.database,
            file_store=self.store,
            library_service=self.library,
            secret_store=self.secret_store,
            worker=self.worker,
            client=object(),
            repository=EmptyCentralRepository(),
            resolver=SharedPathResolver(Path(self.temp.name) / "shared"),
            actor=fake_ai_user(),
        )
        self.window.show()
        self.page = self.window.batch_dress_page
        self.window._select_page(self.page)
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def _image(self, name: str, color: tuple[int, int, int]) -> Path:
        return make_image(Path(self.temp.name) / name, color)

    def _add_pair(self, garment: Path, person: Path) -> None:
        self.page._append_assets("garment", [garment])
        self.page._append_assets("person", [person])
        self.page._add_pair(self.page.garment_assets[0], self.page.person_assets[0])

    def test_navigation_contains_batch_dress_up(self) -> None:
        labels = [
            self.window.nav_list.item(index).text()
            for index in range(self.window.nav_list.count())
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
        self.assertTrue(
            any(page is self.page for _, page in self.window.page_specs)
        )
        self.assertEqual(self.page.model_combo.currentData(), "auto")

    def test_folder_scan_uses_nested_paths_and_natural_order(self) -> None:
        root = Path(self.temp.name) / "服装文件夹"
        nested = root / "子目录"
        nested.mkdir(parents=True)
        for name in ("10.png", "2.png"):
            make_image(root / name, (int(name[:-4]), 20, 30))
        make_image(nested / "1.png", (90, 20, 30))
        (root / "说明.txt").write_text("忽略", encoding="utf-8")

        images, errors = scan_image_inputs([root])
        self.assertEqual(errors, [])
        self.assertEqual(
            [path.relative_to(root).as_posix() for path in images],
            ["2.png", "10.png", "子目录/1.png"],
        )
        self.assertLess(
            natural_sort_key("2.png"),
            natural_sort_key("10.png"),
        )

    def test_folder_drop_and_folder_button_share_recursive_import(self) -> None:
        garment_root = Path(self.temp.name) / "服装文件夹"
        garment_nested = garment_root / "系列"
        garment_nested.mkdir(parents=True)
        make_image(garment_nested / "1.png", (20, 40, 80))
        with patch(
            "yixin.ui.batch_dress_page.QFileDialog.getExistingDirectory",
            return_value=str(garment_root),
        ):
            self.page._add_image_folder("garment")
        self.assertEqual(
            [image.original_name for image in self.page.garment_assets],
            ["1.png"],
        )

        person_root = Path(self.temp.name) / "人物文件夹"
        nested = person_root / "员工"
        nested.mkdir(parents=True)
        make_image(nested / "1.png", (10, 20, 30))
        make_image(person_root / "2.png", (10, 30, 20))

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(person_root))])
        event = QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.page.person_list.dropEvent(event)
        self.app.processEvents()
        self.assertEqual(
            [image.original_name for image in self.page.person_assets],
            ["2.png", "1.png"],
        )
        settings = AppSettings(self.database)
        self.assertEqual(settings.input_folder(INPUT_BATCH_GARMENT), garment_root)
        self.assertEqual(settings.input_folder(INPUT_BATCH_PERSON), person_root)

    def test_external_folder_drop_works_on_list_viewport_and_asset_group(
        self,
    ) -> None:
        self.window._select_page(self.page)
        self.window.show()
        self.app.processEvents()
        garment_root = Path(self.temp.name) / "viewport服装"
        garment_root.mkdir()
        make_image(garment_root / "1.png", (20, 60, 80))
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(garment_root))])
        enter_event = QDragEnterEvent(
            QPoint(8, 8),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(self.page.garment_list.viewport(), enter_event)
        move_event = QDragMoveEvent(
            QPoint(8, 8),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(self.page.garment_list.viewport(), move_event)
        event = QDropEvent(
            QPointF(8, 8),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(self.page.garment_list.viewport(), event)
        self.app.processEvents()
        self.assertEqual(
            [image.original_name for image in self.page.garment_assets],
            ["1.png"],
        )

        person_root = Path(self.temp.name) / "group人物"
        person_root.mkdir()
        make_image(person_root / "2.png", (90, 40, 30))
        group_mime = QMimeData()
        group_mime.setUrls([QUrl.fromLocalFile(str(person_root))])
        group_enter = QDragEnterEvent(
            QPoint(8, 8),
            Qt.DropAction.CopyAction,
            group_mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(self.page.person_group, group_enter)
        group_move = QDragMoveEvent(
            QPoint(8, 8),
            Qt.DropAction.CopyAction,
            group_mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(self.page.person_group, group_move)
        group_event = QDropEvent(
            QPointF(8, 8),
            Qt.DropAction.CopyAction,
            group_mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(self.page.person_group, group_event)
        self.app.processEvents()
        self.assertEqual(
            [image.original_name for image in self.page.person_assets],
            ["2.png"],
        )

    def test_garment_mouse_drag_starts_copy_payload(self) -> None:
        self.page._append_assets(
            "garment",
            [self._image("鼠标拖拽服装.png", (20, 50, 80))],
        )
        captured: dict[str, object] = {}

        class FakeDrag:
            def __init__(self, source) -> None:
                captured["source"] = source

            def setMimeData(self, mime) -> None:
                captured["mime"] = mime

            def setPixmap(self, pixmap) -> None:
                captured["pixmap"] = pixmap

            def setHotSpot(self, point) -> None:
                captured["hotspot"] = point

            def exec(self, action):
                captured["action"] = action
                return Qt.DropAction.CopyAction

        item_rect = self.page.garment_list.visualItemRect(
            self.page.garment_list.item(0)
        )
        with patch("yixin.ui.batch_dress_page.QDrag", FakeDrag):
            QTest.mousePress(
                self.page.garment_list.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                item_rect.center(),
            )
            QTest.mouseMove(
                self.page.garment_list.viewport(),
                item_rect.center()
                + QPoint(
                    QApplication.startDragDistance() + 4,
                    QApplication.startDragDistance() + 4,
                ),
                20,
            )
            QTest.mouseRelease(
                self.page.garment_list.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                item_rect.center(),
            )
        self.assertEqual(captured["action"], Qt.DropAction.CopyAction)
        mime = captured["mime"]
        self.assertEqual(
            bytes(mime.data(DRESS_UP_MIME)).decode("utf-8"),
            self.page.garment_assets[0].content_hash,
        )

    def test_fifty_asset_limit_and_duplicate_content_hash(self) -> None:
        root = Path(self.temp.name) / "大量服装"
        root.mkdir()
        for index in range(51):
            make_image(root / f"{index:02d}.png", (index, 20, 30))
        with patch("yixin.ui.batch_dress_page.QMessageBox.information") as information:
            self.page._append_assets("garment", [root])
            self.page._append_assets("garment", [root / "00.png"])
        self.assertEqual(len(self.page.garment_assets), 50)
        self.assertEqual(self.page.garment_list.count(), 50)
        self.assertGreaterEqual(information.call_count, 1)

    def test_click_pairing_direction_and_duplicate_pair(self) -> None:
        garment_one = self._image("服装1.png", (20, 40, 80))
        garment_two = self._image("服装2.png", (50, 70, 90))
        person_one = self._image("人物1.png", (100, 40, 20))
        person_two = self._image("人物2.png", (120, 60, 30))
        self.page._append_assets("garment", [garment_one, garment_two])
        self.page._append_assets("person", [person_one, person_two])

        person_rect = self.page.person_list.visualItemRect(
            self.page.person_list.item(0)
        )
        QTest.mouseClick(
            self.page.person_list.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            person_rect.center(),
        )
        self.assertEqual(self.page.pairs, [])

        garment_rect = self.page.garment_list.visualItemRect(
            self.page.garment_list.item(0)
        )
        QTest.mouseClick(
            self.page.garment_list.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            garment_rect.center(),
        )
        QTest.mouseClick(
            self.page.person_list.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            person_rect.center(),
        )
        self.assertEqual(len(self.page.pairs), 1)

        self.page._add_pair(
            self.page.garment_assets[0],
            self.page.person_assets[0],
        )
        self.assertEqual(len(self.page.pairs), 1)
        self.page._add_pair(
            self.page.garment_assets[0],
            self.page.person_assets[1],
        )
        self.assertEqual(len(self.page.pairs), 2)

    def test_auto_match_creates_one_group_per_person_and_replaces_existing(
        self,
    ) -> None:
        garments = [
            self._image(f"自动服装{index}.png", (20 + index * 20, 50, 80))
            for index in range(3)
        ]
        people = [
            self._image(f"自动人物{index}.png", (100, 30 + index * 20, 20))
            for index in range(5)
        ]
        self.page._append_assets("garment", garments)
        self.page._append_assets("person", people)
        self.page._add_pair(
            self.page.garment_assets[0],
            self.page.person_assets[0],
        )
        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            self.page._auto_match()
        self.assertEqual(len(self.page.pairs), 1)
        self.assertEqual(self.page.pairs[0].kind, "manual")

        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.page._auto_match()
        self.assertEqual(len(self.page.pairs), 5)
        for index, group in enumerate(self.page.pairs):
            self.assertEqual(group.kind, "auto")
            self.assertEqual(group.garment_count, 3)
            self.assertEqual(
                [image.content_hash for image in group.references[:3]],
                [image.content_hash for image in self.page.garment_assets],
            )
            self.assertEqual(
                group.references[-1].content_hash,
                self.page.person_assets[index].content_hash,
            )
        self.assertEqual(self.database.list_tasks(), [])

    def test_auto_match_splits_more_than_thirteen_garments(self) -> None:
        garments = [
            self._image(f"拆分服装{index:02d}.png", (index, 60, 90))
            for index in range(20)
        ]
        people = [
            self._image(f"拆分人物{index}.png", (100, 40 + index, 20))
            for index in range(2)
        ]
        self.page._append_assets("garment", garments)
        self.page._append_assets("person", people)
        self.page._auto_match()
        self.assertEqual(len(self.page.pairs), 4)
        self.assertEqual(
            [len(group.references) for group in self.page.pairs],
            [14, 8, 14, 8],
        )
        for group in self.page.pairs:
            self.assertLessEqual(len(group.references), 14)
            self.assertEqual(group.roles[-1], "person")

    def test_auto_groups_update_on_crop_and_remove_with_garment(self) -> None:
        garments = [
            self._image(f"联动服装{index}.png", (20 + index * 30, 70, 100))
            for index in range(2)
        ]
        people = [
            self._image(f"联动人物{index}.png", (120, 40 + index * 20, 30))
            for index in range(2)
        ]
        self.page._append_assets("garment", garments)
        self.page._append_assets("person", people)
        self.page._auto_match()
        original = self.page.garment_assets[0]
        self.page.garment_list.setCurrentRow(0)

        class FakeCropDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, *args, **kwargs) -> None:
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            @staticmethod
            def crop_box():
                return (5, 5, 45, 65)

        with patch("yixin.ui.batch_dress_page.CropDialog", FakeCropDialog):
            self.page._crop_selected("garment")
        cropped = self.page.garment_assets[0]
        self.assertNotEqual(cropped.content_hash, original.content_hash)
        for group in self.page.pairs:
            self.assertIn(
                cropped.content_hash,
                [image.content_hash for image in group.references],
            )
            self.assertNotIn(
                original.content_hash,
                [image.content_hash for image in group.references],
            )

        self.page.garment_list.setCurrentRow(0)
        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.page._remove_selected("garment")
        self.assertEqual(self.page.pairs, [])

    def test_free_compose_dialog_limits_deduplicates_and_reorders(self) -> None:
        garments = [
            self._image(f"自由服装{index}.png", (20 + index * 30, 70, 110))
            for index in range(8)
        ]
        people = [
            self._image(f"自由人物{index}.png", (140, 30 + index * 20, 40))
            for index in range(8)
        ]
        self.page._append_assets("garment", garments)
        self.page._append_assets("person", people)
        dialog = FreeComposeDialog(
            self.page.garment_assets,
            self.page.person_assets,
        )
        dialog.available_list.selectAll()
        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.information"
        ) as information:
            dialog._add_selected()
        self.assertEqual(dialog.selected_list.count(), 14)
        information.assert_called_once()
        selected = dialog.selected_items()
        first_role, first_image = selected[0]
        dialog.selected_list.setCurrentRow(0)
        dialog._move_selected(1)
        self.assertEqual(
            dialog.selected_list.currentItem().data(Qt.ItemDataRole.UserRole),
            (first_role, first_image),
        )
        dialog.available_list.setCurrentRow(0)
        with patch("yixin.ui.batch_dress_page.QMessageBox.information"):
            dialog._add_selected()
        self.assertEqual(dialog.selected_list.count(), 14)
        dialog.close()

    def test_free_compose_appends_group_and_adds_source_prompt(self) -> None:
        garment_one = self._image("自由组合服装1.png", (20, 50, 80))
        garment_two = self._image("自由组合服装2.png", (50, 80, 110))
        person = self._image("自由组合人物.png", (120, 70, 30))
        self.page._append_assets("garment", [garment_one, garment_two])
        self.page._append_assets("person", [person])
        self.page._add_pair(
            self.page.garment_assets[0],
            self.page.person_assets[0],
        )

        class FakeFreeDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, *args, **kwargs) -> None:
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            @staticmethod
            def selected_items():
                return [
                    ("person", self_page.person_assets[0]),
                    ("garment", self_page.garment_assets[1]),
                    ("garment", self_page.garment_assets[0]),
                ]

        self_page = self.page
        with patch(
            "yixin.ui.batch_dress_page.FreeComposeDialog",
            FakeFreeDialog,
        ):
            self.page._free_compose()
        self.assertEqual(len(self.page.pairs), 2)
        group = self.page.pairs[-1]
        self.assertEqual(group.kind, "free")
        self.assertEqual(
            group.references,
            (
                self.page.person_assets[0],
                self.page.garment_assets[1],
                self.page.garment_assets[0],
            ),
        )
        self.assertEqual(
            self.page._prompt_for_group("用户要求", group),
            "自由组合：第 1 张为人物素材，第 2 张为服装素材，第 3 张为服装素材。\n用户要求",
        )

    def test_auto_and_free_tasks_store_final_prompt_and_modes(self) -> None:
        garments = [
            self._image(f"提示服装{index}.png", (20 + index * 20, 50, 80))
            for index in range(2)
        ]
        person = self._image("提示人物.png", (100, 40, 20))
        self.page._append_assets("garment", garments)
        self.page._append_assets("person", [person])
        self.page._auto_match()
        self.page.prompt_edit.setPlainText("保持人物一致")
        with (
            patch(
                "yixin.ui.batch_dress_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch("yixin.ui.batch_dress_page.QMessageBox.information"),
        ):
            self.page._enqueue_all()
        tasks = self.database.list_tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["mode"], MODE_DRESS_UP)
        self.assertTrue(task["prompt"].startswith("自动匹配："))
        self.assertTrue(task["prompt"].endswith("保持人物一致"))
        references = self.database.get_task_references(task["id"])
        self.assertEqual(len(references), 3)
        self.assertEqual(
            references[-1].content_hash,
            self.page.person_assets[0].content_hash,
        )

        free_group = DressTaskGroup(
            references=(
                self.page.person_assets[0],
                self.page.garment_assets[0],
            ),
            roles=("person", "garment"),
            kind="free",
            auto_prompt=True,
        )
        self.page.pairs = [free_group]
        self.page.prompt_edit.setPlainText("自由处理")
        with (
            patch(
                "yixin.ui.batch_dress_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch("yixin.ui.batch_dress_page.QMessageBox.information"),
        ):
            self.page._enqueue_all()
        tasks = self.database.list_tasks()
        self.assertEqual(len(tasks), 2)
        free_task = tasks[0]
        self.assertEqual(free_task["mode"], MODE_FREE_COMPOSE)
        self.assertTrue(free_task["prompt"].startswith("自由组合："))

    def test_free_compose_retry_restores_order_roles_and_user_prompt(self) -> None:
        first = self.store.save_reference(
            self._image("重试自由人物.png", (100, 50, 20))
        )
        second = self.store.save_reference(
            self._image("重试自由服装.png", (20, 50, 80))
        )
        task_id = self.database.create_task(
            ImageRequest(
                model="gemini-3-pro-image",
                prompt=(
                    "自由组合：第 1 张为人物素材，第 2 张为服装素材。\n"
                    "修改后重新生成"
                ),
                reference_images=(first, second),
                mode=MODE_FREE_COMPOSE,
            )
        )
        claimed = self.database.claim_next_queued()
        self.database.transition_task(int(claimed["id"]), TASK_SUCCESS)
        history = self.window.history_page
        history.refresh()
        history.show_task(task_id)
        history._retry_current()
        self.app.processEvents()

        self.assertIs(self.window.stack.currentWidget(), self.page)
        self.assertEqual(len(self.page.pairs), 1)
        group = self.page.pairs[0]
        self.assertEqual(group.kind, "free")
        self.assertEqual(group.roles, ("person", "garment"))
        self.assertEqual(self.page.prompt_edit.toPlainText(), "修改后重新生成")
        self.assertEqual(
            [image.content_hash for image in group.references],
            [first.content_hash, second.content_hash],
        )

    def test_drag_drop_payload_creates_pair_for_target_person(self) -> None:
        self.page._append_assets(
            "garment",
            [self._image("拖拽服装.png", (20, 50, 80))],
        )
        self.page._append_assets(
            "person",
            [self._image("拖拽人物.png", (100, 50, 20))],
        )
        item_rect = self.page.person_list.visualItemRect(
            self.page.person_list.item(0)
        )
        mime = QMimeData()
        mime.setData(
            DRESS_UP_MIME,
            self.page.garment_assets[0].content_hash.encode("utf-8"),
        )
        event = QDropEvent(
            QPointF(item_rect.center()),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.page.person_list.dropEvent(event)
        self.app.processEvents()
        self.assertEqual(len(self.page.pairs), 1)
        self.assertEqual(
            self.page.pairs[0].garment.content_hash,
            self.page.garment_assets[0].content_hash,
        )
        self.assertEqual(
            self.page.pairs[0].person.content_hash,
            self.page.person_assets[0].content_hash,
        )

    def test_removing_paired_asset_removes_related_pairs(self) -> None:
        garment = self._image("移除服装.png", (20, 50, 80))
        person = self._image("移除人物.png", (100, 50, 20))
        self._add_pair(garment, person)
        self.page.garment_list.setCurrentRow(0)
        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.page._remove_selected("garment")
        self.assertEqual(self.page.garment_assets, [])
        self.assertEqual(self.page.pairs, [])

    def test_garment_crop_updates_pair_and_restore_returns_original(self) -> None:
        garment = self._image("裁剪服装.png", (20, 50, 80))
        person = self._image("裁剪人物.png", (100, 50, 20))
        self._add_pair(garment, person)
        original = self.page.garment_assets[0]
        self.page.garment_list.setCurrentRow(0)
        self.assertTrue(self.page.garment_crop_button.isEnabled())
        self.assertTrue(self.page.garment_restore_button.isEnabled())

        class FakeCropDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, *args, **kwargs) -> None:
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            @staticmethod
            def crop_box():
                return (10, 10, 50, 50)

        with patch("yixin.ui.batch_dress_page.CropDialog", FakeCropDialog):
            self.page._crop_selected("garment")
        cropped = self.page.garment_assets[0]
        self.assertNotEqual(cropped.content_hash, original.content_hash)
        self.assertEqual((cropped.width, cropped.height), (40, 40))
        self.assertEqual(cropped.source_path, garment.resolve())
        self.assertTrue(garment.is_file())
        self.assertEqual(self.page.garment_list.currentRow(), 0)
        self.assertEqual(len(self.page.pairs), 1)
        self.assertEqual(
            self.page.pairs[0].garment.content_hash,
            cropped.content_hash,
        )
        self.assertEqual(
            self.page.pairs[0].person.content_hash,
            self.page.person_assets[0].content_hash,
        )

        self.page._restore_selected("garment")
        restored = self.page.garment_assets[0]
        self.assertEqual(restored.content_hash, original.content_hash)
        self.assertEqual(
            self.page.pairs[0].garment.content_hash,
            restored.content_hash,
        )
        self.assertEqual(self.page.garment_list.currentRow(), 0)

    def test_person_crop_updates_pair_without_changing_garment(self) -> None:
        garment = self._image("人物裁剪服装.png", (20, 50, 80))
        person = self._image("人物裁剪素材.png", (100, 50, 20))
        self._add_pair(garment, person)
        original_garment = self.page.garment_assets[0]
        self.page.person_list.setCurrentRow(0)
        self.assertTrue(self.page.person_crop_button.isEnabled())

        class FakeCropDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, *args, **kwargs) -> None:
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            @staticmethod
            def crop_box():
                return (5, 5, 45, 65)

        with patch("yixin.ui.batch_dress_page.CropDialog", FakeCropDialog):
            self.page._crop_selected("person")
        cropped = self.page.person_assets[0]
        self.assertEqual(self.page.person_list.currentRow(), 0)
        self.assertEqual(
            self.page.pairs[0].person.content_hash,
            cropped.content_hash,
        )
        self.assertEqual(
            self.page.pairs[0].garment.content_hash,
            original_garment.content_hash,
        )

    def test_asset_action_buttons_follow_selection_and_source_availability(self) -> None:
        garment = self._image("按钮状态.png", (20, 50, 80))
        self.page._append_assets("garment", [garment])
        self.assertFalse(self.page.garment_crop_button.isEnabled())
        self.assertFalse(self.page.garment_restore_button.isEnabled())
        self.page.garment_list.setCurrentRow(0)
        self.assertTrue(self.page.garment_crop_button.isEnabled())
        self.assertTrue(self.page.garment_restore_button.isEnabled())
        garment.unlink()
        self.page._update_asset_actions()
        self.assertTrue(self.page.garment_crop_button.isEnabled())
        self.assertFalse(self.page.garment_restore_button.isEnabled())

    def test_duplicate_crop_result_is_rejected_without_changing_pairs(self) -> None:
        first = self._image("重复裁剪1.png", (20, 50, 80))
        second = self._image("重复裁剪2.png", (70, 80, 90))
        person = self._image("重复人物.png", (110, 50, 20))
        self.page._append_assets("garment", [first, second])
        self.page._append_assets("person", [person])
        self.page._add_pair(
            self.page.garment_assets[0],
            self.page.person_assets[0],
        )
        before_assets = list(self.page.garment_assets)
        before_pairs = list(self.page.pairs)
        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.information"
        ) as information:
            replaced = self.page._replace_asset(
                "garment",
                0,
                self.page.garment_assets[1],
                action_label="裁剪",
            )
        self.assertFalse(replaced)
        self.assertEqual(self.page.garment_assets, before_assets)
        self.assertEqual(self.page.pairs, before_pairs)
        information.assert_called_once()

    def test_pair_area_uses_four_columns_and_submit_bar_stays_fixed(self) -> None:
        garments = [
            self._image(f"展开服装{index}.png", (20 + index, 50, 80))
            for index in range(3)
        ]
        people = [
            self._image(f"展开人物{index}.png", (100, 30 + index, 20))
            for index in range(2)
        ]
        self.page._append_assets("garment", garments)
        self.page._append_assets("person", people)
        combinations = [
            (garment, person)
            for garment in self.page.garment_assets
            for person in self.page.person_assets
        ]
        for garment, person in combinations[:4]:
            self.page._add_pair(garment, person)
        self.assertEqual(self.page.pair_grid.rowCount(), 1)
        for garment, person in combinations[4:]:
            self.page._add_pair(garment, person)
        self.assertEqual(self.page.pair_grid.rowCount(), 2)
        self.assertTrue(hasattr(self.page, "pair_scroll"))
        self.assertIs(self.page.pair_scroll.widget(), self.page.pair_host)
        self.assertEqual(
            self.page.pair_scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertIs(self.page.pair_group.parent(), self.page)
        self.assertIsNot(self.page.page_scroll.widget(), self.page.pair_group)
        self.assertIs(self.page.submit_card.parent(), self.page)
        self.assertIs(self.page.generate_button.parent(), self.page.submit_card)
        first_card = self.page.pair_grid.itemAt(0).widget()
        thumbnail = first_card.findChild(QLabel, "DressPairThumbnail")
        self.assertGreaterEqual(thumbnail.width(), 64)
        self.assertGreaterEqual(thumbnail.height(), 64)

        self.window._select_page(self.page)
        self.window.resize(1900, 1000)
        self.app.processEvents()
        self.assertGreaterEqual(self.page.pair_scroll.height(), 200)
        expected_columns = max(
            2,
            min(
                8,
                (
                    self.page.pair_scroll.viewport().width()
                    + PAIR_CARD_GAP
                )
                // (PAIR_CARD_MIN_WIDTH + PAIR_CARD_GAP),
            ),
        )
        self.assertEqual(self.page.pair_columns, expected_columns)
        self.assertGreaterEqual(
            self.page.pair_group.height() / self.page.height(),
            0.30,
        )
        submit_top = self.page.submit_card.mapTo(self.page, QPoint(0, 0)).y()
        pair_bottom = self.page.pair_group.mapTo(
            self.page,
            QPoint(0, self.page.pair_group.height()),
        ).y()
        self.assertGreaterEqual(submit_top, pair_bottom)
        self.assertLessEqual(submit_top - pair_bottom, 12)
        pair_top = self.page.pair_group.mapTo(self.page, QPoint(0, 0)).y()
        scrollbar = self.page.page_scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.app.processEvents()
        self.assertEqual(
            self.page.submit_card.mapTo(self.page, QPoint(0, 0)).y(),
            submit_top,
        )
        self.assertEqual(
            self.page.pair_group.mapTo(self.page, QPoint(0, 0)).y(),
            pair_top,
        )

    def test_default_window_shows_asset_boxes_without_page_scroll(self) -> None:
        garments = [
            self._image(f"首屏服装{index}.png", (20 + index * 20, 50, 80))
            for index in range(3)
        ]
        people = [
            self._image(f"首屏人物{index}.png", (100, 30 + index * 20, 20))
            for index in range(3)
        ]
        self.page._append_assets("garment", garments)
        self.page._append_assets("person", people)
        self.window._select_page(self.page)
        self.window.resize(1480, 920)
        self.window.show()
        self.app.processEvents()
        self.app.processEvents()

        self.assertEqual(self.page.page_scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(self.page.pair_group.height(), 260)
        self.assertGreaterEqual(self.page.garment_list.minimumHeight(), 160)
        settings_top = self.page.settings_group.mapTo(
            self.page,
            QPoint(0, 0),
        )
        prompt_top = self.page.prompt_group.mapTo(
            self.page,
            QPoint(0, 0),
        )
        self.assertLessEqual(abs(settings_top.y() - prompt_top.y()), 2)
        self.assertLess(settings_top.x(), prompt_top.x())
        garment_top = self.page.garment_group.mapTo(
            self.page,
            QPoint(0, 0),
        )
        person_top = self.page.person_group.mapTo(
            self.page,
            QPoint(0, 0),
        )
        self.assertLessEqual(
            abs(self.page.settings_group.width() - self.page.garment_group.width()),
            2,
        )
        self.assertLessEqual(
            abs(self.page.prompt_group.width() - self.page.person_group.width()),
            2,
        )
        self.assertLessEqual(abs(settings_top.x() - garment_top.x()), 2)
        self.assertLessEqual(abs(prompt_top.x() - person_top.x()), 2)
        asset_bottom = self.page.asset_columns_host.mapTo(
            self.page.page_scroll.viewport(),
            QPoint(0, self.page.asset_columns_host.height()),
        ).y()
        self.assertLessEqual(
            asset_bottom,
            self.page.page_scroll.viewport().height(),
        )

        self.window.resize(1900, 1000)
        self.app.processEvents()
        self.app.processEvents()
        self.assertAlmostEqual(
            self.page.pair_group.height() / self.page.height(),
            0.40,
            delta=0.02,
        )

    def test_template_saved_in_create_is_available_in_batch(self) -> None:
        create = self.window.create_page
        create.prompt_edit.setPlainText("创作页保存的通用模板")
        with patch(
            "yixin.ui.create_page.QInputDialog.getText",
            return_value=("通用模板", True),
        ):
            create._quick_save_template()
        self.app.processEvents()
        batch = self.page
        matching_index = next(
            index
            for index in range(batch.template_combo.count())
            if (
                batch.template_combo.itemData(index)
                and batch.template_combo.itemData(index)["name"] == "通用模板"
            )
        )
        batch.template_combo.setCurrentIndex(matching_index)
        batch._apply_template()
        self.assertEqual(batch.prompt_edit.toPlainText(), "创作页保存的通用模板")
        self.assertEqual(self.database.list_tasks(), [])

    def test_builtin_dress_templates_are_shared_and_editable(self) -> None:
        batch_names = [
            self.page.template_combo.itemText(index)
            for index in range(self.page.template_combo.count())
        ]
        create_names = [
            self.window.create_page.template_combo.itemText(index)
            for index in range(self.window.create_page.template_combo.count())
        ]
        for name in ("内置·基础服装替换", "内置·多图换装"):
            self.assertIn(name, batch_names)
            self.assertIn(name, create_names)
        index = self.page.template_combo.findText("内置·基础服装替换")
        self.page.template_combo.setCurrentIndex(index)
        self.app.processEvents()
        self.assertIn(
            "【图2】中的人物的上衣改为【图1】中的服装",
            self.page.prompt_edit.toPlainText(),
        )
        self.page.prompt_edit.append("用户补充")
        self.assertIn("用户补充", self.page.prompt_edit.toPlainText())

    def test_batch_template_switch_is_immediate_and_refresh_is_safe(self) -> None:
        self.database.save_template("批量模板A", "批量提示词 A")
        self.database.save_template("批量模板B", "批量提示词 B")
        self.page.refresh_templates()
        index_a = self.page.template_combo.findText("批量模板A")
        index_b = self.page.template_combo.findText("批量模板B")
        self.page.template_combo.setCurrentIndex(index_a)
        self.app.processEvents()
        self.assertEqual(self.page.prompt_edit.toPlainText(), "批量提示词 A")
        self.page.template_combo.setCurrentIndex(index_b)
        self.app.processEvents()
        self.assertEqual(self.page.prompt_edit.toPlainText(), "批量提示词 B")

        self.page.prompt_edit.setPlainText("批量用户修改")
        self.page.refresh_templates()
        self.assertEqual(self.page.prompt_edit.toPlainText(), "批量用户修改")
        self.page.template_combo.setCurrentIndex(0)
        self.assertEqual(self.page.prompt_edit.toPlainText(), "批量用户修改")
        self.assertNotIn(
            "应用",
            [button.text() for button in self.page.findChildren(QPushButton)],
        )

    def test_template_saved_in_batch_is_available_in_create(self) -> None:
        batch = self.page
        batch.prompt_edit.setPlainText("换装页保存的通用模板")
        with patch(
            "yixin.ui.batch_dress_page.QInputDialog.getText",
            return_value=("换装模板", True),
        ):
            batch._quick_save_template()
        self.app.processEvents()
        create = self.window.create_page
        matching_index = next(
            index
            for index in range(create.template_combo.count())
            if (
                create.template_combo.itemData(index)
                and create.template_combo.itemData(index)["name"] == "换装模板"
            )
        )
        create.template_combo.setCurrentIndex(matching_index)
        create._apply_template()
        self.assertEqual(
            create.prompt_edit.toPlainText(),
            "换装页保存的通用模板",
        )
        self.assertEqual(self.database.list_tasks(), [])

    def test_batch_template_manager_can_apply_and_sync_changes(self) -> None:
        class FakeTemplateManager:
            def __init__(self, *args, **kwargs) -> None:
                self.templates_changed = True
                self.selected_prompt = "管理窗口选中的提示词"

            def exec(self):
                return QDialog.DialogCode.Accepted

        batch = self.page
        with patch(
            "yixin.ui.batch_dress_page.TemplateManagerDialog",
            FakeTemplateManager,
        ):
            batch._manage_templates()
        self.app.processEvents()
        self.assertEqual(batch.prompt_edit.toPlainText(), "管理窗口选中的提示词")
        self.assertTrue(hasattr(self.window.create_page, "refresh_templates"))

    def test_empty_batch_template_prompt_is_rejected(self) -> None:
        batch = self.page
        batch.prompt_edit.clear()
        with (
            patch(
                "yixin.ui.batch_dress_page.QInputDialog.getText"
            ) as get_text,
            patch(
                "yixin.ui.batch_dress_page.QMessageBox.information"
            ) as information,
        ):
            batch._quick_save_template()
        get_text.assert_not_called()
        information.assert_called_once()
        self.assertEqual(
            {item["name"] for item in self.database.list_templates()},
            {name for name, _ in BUILTIN_PROMPT_TEMPLATES},
        )

    def test_deleted_selected_template_returns_combo_to_default(self) -> None:
        template_id = self.database.save_template("待删除模板", "旧提示词")
        batch = self.page
        batch.refresh_templates()
        selected_index = next(
            index
            for index in range(batch.template_combo.count())
            if (
                batch.template_combo.itemData(index)
                and batch.template_combo.itemData(index)["id"] == template_id
            )
        )
        batch.template_combo.setCurrentIndex(selected_index)
        self.database.delete_template(template_id)
        batch.refresh_templates()
        self.assertEqual(batch.template_combo.currentIndex(), 0)
        self.assertIsNone(batch.template_combo.currentData())

    def test_many_pairs_scroll_inside_pair_area_without_horizontal_overflow(self) -> None:
        garments = [
            self._image(f"多配对服装{index}.png", (20 + index * 20, 50, 80))
            for index in range(5)
        ]
        people = [
            self._image(f"多配对人物{index}.png", (100, 25 + index * 20, 20))
            for index in range(4)
        ]
        self.page._append_assets("garment", garments)
        self.page._append_assets("person", people)
        for garment in self.page.garment_assets:
            for person in self.page.person_assets:
                self.page._add_pair(garment, person)

        self.window._select_page(self.page)
        self.window.resize(1180, 720)
        self.window.show()
        self.app.processEvents()
        self.app.processEvents()
        expected_columns = max(
            2,
            min(
                8,
                (
                    self.page.pair_scroll.viewport().width()
                    + PAIR_CARD_GAP
                )
                // (PAIR_CARD_MIN_WIDTH + PAIR_CARD_GAP),
            ),
        )
        self.assertEqual(self.page.pair_columns, expected_columns)
        self.assertEqual(
            self.page.pair_grid.rowCount(),
            (len(self.page.pairs) + expected_columns - 1) // expected_columns,
        )
        self.assertGreater(self.page.pair_scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(self.page.pair_scroll.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(
            self.page.pair_host.sizeHint().width(),
            self.page.pair_scroll.viewport().width(),
        )

    def test_batch_enqueue_creates_independent_ordered_tasks(self) -> None:
        garment_one = self._image("批量服装1.png", (20, 50, 80))
        garment_two = self._image("批量服装2.png", (50, 80, 110))
        person = self._image("批量人物.png", (120, 70, 30))
        self.page._append_assets("garment", [garment_one, garment_two])
        self.page._append_assets("person", [person])
        self.page._add_pair(
            self.page.garment_assets[0],
            self.page.person_assets[0],
        )
        self.page._add_pair(
            self.page.garment_assets[1],
            self.page.person_assets[0],
        )
        self.page.prompt_edit.setPlainText("保持人物不变，换成参考服装")
        self.page.model_combo.setCurrentIndex(
            self.page.model_combo.findData("gemini-nano-banana-2.1")
        )
        self.page.aspect_combo.setCurrentText("16:9")
        self.page.resolution_combo.setCurrentText("4K")
        self.page.count_spin.setValue(2)
        emitted: list[int] = []
        self.page.tasks_enqueued.connect(emitted.append)

        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch("yixin.ui.batch_dress_page.QMessageBox.information"):
            self.page._enqueue_all()

        tasks = list(reversed(self.database.list_tasks()))
        self.assertEqual(len(tasks), 2)
        self.assertEqual(emitted, [2])
        self.assertEqual(self.page.pairs, [])
        self.assertEqual(len(self.page.garment_assets), 2)
        for index, task in enumerate(tasks):
            self.assertEqual(task["mode"], MODE_DRESS_UP)
            self.assertEqual(task["prompt"], "保持人物不变，换成参考服装")
            self.assertEqual(task["model"], "gemini-nano-banana-2.1")
            self.assertIsNone(task["auto_strategy"])
            self.assertEqual(task["aspect_ratio"], "16:9")
            self.assertEqual(task["resolution"], "4K")
            self.assertEqual(task["n"], 2)
            references = self.database.get_task_references(task["id"])
            self.assertEqual(len(references), 2)
            self.assertEqual(
                references[0].content_hash,
                self.store.save_reference(
                    [garment_one, garment_two][index]
                ).content_hash,
            )
            self.assertEqual(
                references[1].content_hash,
                self.store.save_reference(person).content_hash,
            )

    def test_auto_two_image_dress_up_uses_pro(self) -> None:
        garment = self._image("自动策略服装.png", (20, 50, 80))
        person = self._image("自动策略人物.png", (100, 50, 20))
        self.page._append_assets("garment", [garment])
        self.page._append_assets("person", [person])
        self.page._add_pair(
            self.page.garment_assets[0],
            self.page.person_assets[0],
        )
        self.page.prompt_edit.setPlainText("保持人物、背景和服装细节不变")
        self.page.resolution_combo.setCurrentText("4K")
        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch("yixin.ui.batch_dress_page.QMessageBox.information"):
            self.page._enqueue_all()
        task = self.database.list_tasks()[0]
        self.assertEqual(task["model"], "gemini-3-pro-image")
        self.assertEqual(task["auto_strategy"], "quality")
        self.assertIn("参考图编辑", task["auto_reason"])

    def test_cancelled_confirmation_and_empty_prompt_create_nothing(self) -> None:
        garment = self._image("确认服装.png", (20, 50, 80))
        person = self._image("确认人物.png", (100, 50, 20))
        self._add_pair(garment, person)
        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ), patch("yixin.ui.batch_dress_page.QMessageBox.information"):
            self.page._enqueue_all()
        self.assertEqual(self.database.list_tasks(), [])

        self.page.prompt_edit.setPlainText("有效提示词")
        with patch(
            "yixin.ui.batch_dress_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ), patch("yixin.ui.batch_dress_page.QMessageBox.information") as information:
            self.page.prompt_edit.clear()
            self.page._enqueue_all()
        self.assertEqual(self.database.list_tasks(), [])
        information.assert_called_once()

    def test_history_marks_and_retries_batch_task_in_batch_page(self) -> None:
        garment = self.store.save_reference(
            self._image("历史服装.png", (20, 50, 80))
        )
        person = self.store.save_reference(
            self._image("历史人物.png", (100, 50, 20))
        )
        task_id = self.database.create_task(
            ImageRequest(
                model="gemini-3-pro-image",
                prompt="历史批量换装",
                n=1,
                aspect_ratio="3:4",
                resolution="2K",
                reference_images=(garment, person),
                mode=MODE_DRESS_UP,
            )
        )
        claimed = self.database.claim_next_queued()
        self.database.transition_task(int(claimed["id"]), TASK_SUCCESS)

        history = self.window.history_page
        history.refresh()
        history.show_task(task_id)
        row = history._find_task_row(task_id)
        self.assertEqual(history.table.item(row, 2).text(), "批量换装")
        history._retry_current()
        self.app.processEvents()

        self.assertIs(self.window.stack.currentWidget(), self.page)
        self.assertEqual(len(self.database.list_tasks()), 1)
        self.assertEqual(len(self.page.pairs), 1)
        self.assertEqual(self.page.prompt_edit.toPlainText(), "历史批量换装")
        self.assertEqual(
            self.page.pairs[0],
            DressTaskGroup.manual(garment, person),
        )

    def test_batch_task_opens_in_create_queue_without_losing_previews(self) -> None:
        garment = self.store.save_reference(
            self._image("队列服装.png", (20, 50, 80))
        )
        person = self.store.save_reference(
            self._image("队列人物.png", (100, 50, 20))
        )
        task_id = self.database.create_task(
            ImageRequest(
                model="gemini-3-pro-image",
                prompt="队列中的批量换装",
                reference_images=(garment, person),
                mode=MODE_DRESS_UP,
            )
        )
        create_page = self.window.create_page
        create_page.refresh_queue()
        row = next(
            row
            for row in range(create_page.queue_table.rowCount())
            if create_page.queue_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            == task_id
        )
        create_page.queue_table.selectRow(row)
        self.app.processEvents()
        self.assertEqual(create_page.current_task_id, task_id)
        self.assertEqual(create_page.mode_combo.currentData(), "reference")
        self.assertEqual(
            [image.content_hash for image in create_page.reference_images],
            [garment.content_hash, person.content_hash],
        )

    def test_database_bulk_creation_is_all_or_nothing(self) -> None:
        garment = self.store.save_reference(
            self._image("事务服装.png", (20, 50, 80))
        )
        person = self.store.save_reference(
            self._image("事务人物.png", (100, 50, 20))
        )
        valid = ImageRequest(
            model="gemini-3-pro-image",
            prompt="有效任务",
            reference_images=(garment, person),
            mode=MODE_DRESS_UP,
        )
        invalid = ImageRequest(
            model="gemini-3-pro-image",
            prompt="无效任务",
            reference_images=(garment,),
            mode=MODE_DRESS_UP,
        )
        with self.assertRaises(ValueError):
            self.database.create_tasks((valid, invalid))
        self.assertEqual(self.database.list_tasks(), [])


if __name__ == "__main__":
    unittest.main()
