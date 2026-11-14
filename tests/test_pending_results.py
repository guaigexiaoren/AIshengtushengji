from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yixin.collaboration.pending_results import (
    clear_pending_results,
    load_pending_results,
    migrate_legacy_pending_results,
    pending_results_root,
    remove_pending_item,
    save_pending_manifest,
)


class PendingResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_pending(self) -> Path:
        folder = self.root / "pending_results" / "7"
        folder.mkdir(parents=True)
        image = folder / "00_abcdef123456.png"
        image.write_bytes(b"fake-image")
        save_pending_manifest(
            self.root,
            attempt_id=7,
            task_id=3,
            project_relative_root="内部项目/项目/11.10/DA1",
            task_code="账号-DA1-001-01",
            attempt_no=2,
            items=[
                {
                    "result_index": 0,
                    "filename": image.name,
                    "content_hash": "a" * 64,
                    "mime": "image/png",
                    "width": 30,
                    "height": 40,
                }
            ],
        )
        return image

    def test_saves_and_loads_pending_results(self) -> None:
        image = self._create_pending()
        results = load_pending_results(self.root, 7)
        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.path, image)
        self.assertEqual(item.attempt_no, 2)
        self.assertEqual(item.result_index, 0)
        self.assertEqual((item.width, item.height), (30, 40))

    def test_remove_pending_item_deletes_file_and_manifest(self) -> None:
        image = self._create_pending()
        remove_pending_item(self.root, 7, 0)
        self.assertFalse(image.exists())
        self.assertEqual(load_pending_results(self.root, 7), [])
        self.assertFalse(
            (self.root / "pending_results" / "7" / "manifest.json").exists()
        )

    def test_pending_root_is_appended_once(self) -> None:
        self.assertEqual(
            pending_results_root(self.root),
            self.root / "pending_results",
        )
        image = self._create_pending()
        self.assertEqual(
            image.parent,
            self.root / "pending_results" / "7",
        )

    def test_clear_pending_results_removes_attempt_folder(self) -> None:
        self._create_pending()
        clear_pending_results(self.root, 7)
        self.assertFalse((self.root / "pending_results" / "7").exists())

    def test_migrates_legacy_double_nested_results(self) -> None:
        legacy_root = self.root / "pending_results"
        folder = legacy_root / "pending_results" / "7"
        folder.mkdir(parents=True)
        image = folder / "00_abc.png"
        image.write_bytes(b"legacy")
        save_pending_manifest(
            legacy_root,
            attempt_id=7,
            task_id=3,
            project_relative_root="内部项目/项目/11.10/DA1",
            task_code="账号-DA1-001-01",
            attempt_no=1,
            items=[
                {
                    "result_index": 0,
                    "filename": image.name,
                    "content_hash": "b" * 64,
                    "mime": "image/png",
                    "width": 10,
                    "height": 10,
                }
            ],
        )
        migrate_legacy_pending_results(self.root)
        results = load_pending_results(self.root, 7)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].path.is_file())
        self.assertFalse(
            (self.root / "pending_results" / "pending_results").exists()
        )


if __name__ == "__main__":
    unittest.main()
