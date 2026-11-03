from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.qt_test_support import flush_qt_deletes

from PySide6.QtWidgets import QApplication, QLabel, QWidget

from yixin.database import Database
from yixin.local_export import LocalImageExporter
from yixin.models import ImageRequest
from yixin.paths import AppPaths
from yixin.settings import LOCAL_SAVE_REFERENCE, AppSettings
from yixin.storage import FileStore
from yixin.ui.save_helpers import (
    LocalSaveController,
    SavedPathToast,
    show_saved_toast,
)
from tests.helpers import make_image, png_bytes


class LocalExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = AppPaths(self.root / "data")
        self.paths.ensure()
        self.database = Database(self.paths.database)
        self.database.initialize()
        self.store = FileStore(self.paths)
        self.settings = AppSettings(self.database)
        self.exporter = LocalImageExporter(self.database, self.settings)

    def tearDown(self) -> None:
        flush_qt_deletes(self.app)
        self.temp.cleanup()

    @classmethod
    def tearDownClass(cls) -> None:
        flush_qt_deletes(cls.app)

    def _completed_task(self, prompt: str = "人物:海报?") -> tuple[int, int, Path]:
        source = make_image(self.root / "references" / "last.png", (10, 40, 90))
        reference = self.store.save_reference(source)
        task_id = self.database.create_task(
            ImageRequest(
                model="gemini-3-pro-image",
                prompt=prompt,
                n=1,
                aspect_ratio="3:4",
                resolution="2K",
                reference_images=(reference,),
                mode="reference",
            )
        )
        result_data = png_bytes((60, 100, 180), (80, 120))
        generated = self.store.store_generated(task_id, result_data, 0)
        thumbnail = self.store.create_thumbnail(generated.path)
        result_id = self.database.add_task_result(
            task_id, 0, generated, thumbnail_path=thumbnail, remote_url=None
        )
        return task_id, result_id, generated.path

    def test_preferred_directory_modes_and_fallback(self) -> None:
        task_id, _, _ = self._completed_task()
        fixed = self.root / "fixed"
        last = self.root / "last"
        fixed.mkdir()
        last.mkdir()

        self.settings.set_local_save_mode("fixed")
        self.settings.set_fixed_folder(fixed)
        self.assertEqual(self.exporter.preferred_directory(task_id), fixed)

        self.settings.set_local_save_mode("reference")
        self.assertEqual(
            self.exporter.preferred_directory(task_id),
            self.root.resolve() / "AI图",
        )
        self.assertTrue((self.root / "AI图").is_dir())

        with self.database.connection() as connection:
            connection.execute("UPDATE task_assets SET source_path = NULL")
        self.settings.set_last_folder(last)
        self.assertEqual(self.exporter.preferred_directory(task_id), last)

    def test_ai_folder_uses_last_reference_and_reuses_folder(self) -> None:
        first_source = make_image(
            self.root / "客户" / "春季" / "一" / "first.png",
            (10, 30, 60),
        )
        second_source = make_image(
            self.root / "客户" / "秋季" / "人物" / "last.png",
            (60, 30, 10),
        )
        references = (
            self.store.save_reference(first_source),
            self.store.save_reference(second_source),
        )
        task_id = self.database.create_task(
            ImageRequest(
                model="gemini-3-pro-image",
                prompt="最后参考图路径",
                reference_images=references,
                mode="reference",
            )
        )
        self.settings.set_local_save_mode(LOCAL_SAVE_REFERENCE)
        expected = (self.root / "客户" / "秋季" / "AI图").resolve()
        self.assertEqual(self.exporter.preferred_directory(task_id), expected)
        marker = expected / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        self.assertEqual(self.exporter.preferred_directory(task_id), expected)
        self.assertTrue(marker.is_file())

    def test_ai_folder_permission_failure_falls_back_to_last_folder(self) -> None:
        task_id, _, _ = self._completed_task()
        last = self.root / "fallback"
        last.mkdir()
        self.settings.set_local_save_mode(LOCAL_SAVE_REFERENCE)
        self.settings.set_last_folder(last)
        with patch(
            "yixin.local_export.Path.mkdir",
            side_effect=PermissionError("denied"),
        ):
            self.assertEqual(self.exporter.preferred_directory(task_id), last)

    def test_local_save_copies_only_result_and_avoids_overwrite(self) -> None:
        task_id, result_id, result_path = self._completed_task()
        destination = self.root / "exports"
        destination.mkdir()
        first = self.exporter.save_result(task_id, result_id, destination)
        second = self.exporter.save_result(task_id, result_id, destination)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), Path(result_path).read_bytes())
        self.assertEqual(len(list(destination.iterdir())), 2)
        self.assertRegex(first.name, r"^AI图_\d{8}_\d{6}_1\.png$")
        self.assertNotIn("人物", first.name)
        self.assertRegex(second.stem, r"_1_2$")
        self.assertEqual(self.settings.last_folder(), destination.resolve())

    def test_ask_mode_chooses_folder_and_updates_last_folder(self) -> None:
        task_id, result_id, _ = self._completed_task()
        destination = self.root / "ask-export"
        destination.mkdir()
        controller = LocalSaveController(QWidget(), self.exporter, self.settings)
        with patch(
            "yixin.ui.save_helpers.QFileDialog.getExistingDirectory",
            return_value=str(destination),
        ):
            saved = controller.save(task_id, result_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.parent, destination.resolve())
        self.assertEqual(self.settings.last_folder(), destination.resolve())

    def test_invalid_fixed_folder_prompts_and_updates_setting(self) -> None:
        task_id, result_id, _ = self._completed_task()
        self.settings.set_local_save_mode("fixed")
        self.settings.set_fixed_folder(self.root / "missing")
        destination = self.root / "replacement"
        destination.mkdir()
        controller = LocalSaveController(QWidget(), self.exporter, self.settings)
        with (
            patch(
                "yixin.ui.save_helpers.QFileDialog.getExistingDirectory",
                return_value=str(destination),
            ),
            patch("yixin.ui.save_helpers.QMessageBox.information"),
        ):
            saved = controller.save(task_id, result_id)
        self.assertIsNotNone(saved)
        self.assertEqual(self.settings.fixed_folder(), destination.resolve())

    def test_local_save_toast_is_short_non_blocking_and_replaces_previous(self) -> None:
        host = QWidget()
        host.resize(900, 600)
        host.show()
        self.app.processEvents()
        first_path = self.root / "exports" / "first-output.png"
        first_path.parent.mkdir()
        first_path.write_bytes(png_bytes((20, 40, 60)))
        toast = show_saved_toast(host, first_path)
        self.app.processEvents()
        self.assertIsInstance(toast, SavedPathToast)
        self.assertTrue(toast.isVisible())
        self.assertEqual(toast.timer.interval(), 3000)
        texts = [label.text() for label in toast.findChildren(QLabel)]
        self.assertIn("已保存到本地", texts)
        self.assertIn("first-output.png", texts)
        self.assertNotIn(str(first_path.parent), texts)
        self.assertEqual(toast.toolTip(), str(first_path))

        second_path = first_path.with_name("second-output.png")
        second_path.write_bytes(png_bytes((60, 40, 20)))
        second = show_saved_toast(host, second_path)
        self.assertFalse(toast.isVisible())
        self.app.processEvents()
        self.assertIs(host._saved_path_toast, second)
        with patch(
            "yixin.ui.save_helpers.QDesktopServices.openUrl",
            return_value=True,
        ) as open_url:
            second.open_button.click()
            self.app.processEvents()
        opened = Path(open_url.call_args.args[0].toLocalFile())
        self.assertEqual(opened, second_path.parent)
        self.assertIsNone(host._saved_path_toast)

        third = show_saved_toast(host, first_path)
        third.timer.timeout.emit()
        self.assertFalse(third.isVisible())
        self.app.processEvents()
        self.assertIsNone(host._saved_path_toast)
        host.close()


if __name__ == "__main__":
    unittest.main()
