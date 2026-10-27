from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from yixin.constants import (
    BUILTIN_PROMPT_TEMPLATES,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_RUNNING,
    TASK_SUCCESS,
)
from yixin.database import Database, StateTransitionError
from yixin.library import LibraryService
from yixin.models import ImageRequest
from yixin.paths import AppPaths
from yixin.settings import (
    AUTO_STRATEGY_QUALITY,
    AUTO_STRATEGY_VALUE,
    INPUT_BATCH_GARMENT,
    INPUT_BATCH_PERSON,
    INPUT_CREATE_REFERENCE,
    INPUT_MANUAL_UPLOAD,
    AppSettings,
)
from yixin.storage import FileStore
from tests.helpers import make_image, png_bytes


class DatabaseLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp.name) / "数据 路径")
        self.paths.ensure()
        self.database = Database(self.paths.database)
        self.database.initialize()
        self.store = FileStore(self.paths)
        self.library = LibraryService(self.database, self.store, self.paths)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _reference(self) -> object:
        source = make_image(Path(self.temp.name) / "ref.png", (15, 50, 90))
        return self.store.save_reference(source)

    def _request(self, *, prompt: str = "测试提示词", references=()) -> ImageRequest:
        return ImageRequest(
            model="gemini-3-pro-image",
            prompt=prompt,
            n=1,
            aspect_ratio="3:4",
            resolution="2K",
            reference_images=tuple(references),
            mode="reference" if references else "text",
        )

    def _create_completed_result(self, prompt: str = "作品", color=(20, 30, 40)):
        task_id = self.database.create_task(self._request(prompt=prompt))
        claimed = self.database.claim_next_queued()
        self.assertEqual(claimed["id"], task_id)
        image = self.store.store_generated(task_id, png_bytes(color), 0)
        thumbnail = self.store.create_thumbnail(image.path)
        result_id = self.database.add_task_result(
            task_id,
            0,
            image,
            thumbnail_path=thumbnail,
            remote_url=None,
        )
        task = self.database.get_task(task_id)
        self.database.update_task_from_api(
            task_id,
            request_id=f"req-{task_id}",
            remote_task_id=f"task-{task_id}",
            raw_response='{"data":[]}',
            input_tokens=11,
            output_tokens=22,
        )
        self.database.transition_task(task_id, TASK_SUCCESS)
        return task_id, result_id, image

    def test_state_transitions_and_recovery(self) -> None:
        task_id = self.database.create_task(self._request())
        claimed = self.database.claim_next_queued()
        self.assertEqual(claimed["status"], TASK_RUNNING)
        self.database.transition_task(task_id, TASK_SUCCESS)
        with self.assertRaises(StateTransitionError):
            self.database.transition_task(task_id, TASK_FAILED)

        running_id = self.database.create_task(self._request(prompt="运行中"))
        self.database.claim_next_queued()
        self.database.request_cancel(running_id)
        self.database.recover_after_restart()
        self.assertEqual(self.database.get_task(running_id)["status"], TASK_CANCELLED)

        interrupted_id = self.database.create_task(self._request(prompt="中断"))
        self.database.claim_next_queued()
        self.database.recover_after_restart()
        interrupted = self.database.get_task(interrupted_id)
        self.assertEqual(interrupted["status"], TASK_FAILED)
        self.assertIn("意外退出", interrupted["error_message"])

    def test_queued_task_can_be_cancelled(self) -> None:
        reference = self._reference()
        task_id = self.database.create_task(
            self._request(prompt="取消任务", references=(reference,))
        )
        self.assertEqual(self.database.request_cancel(task_id), TASK_CANCELLED)
        self.assertEqual(self.database.get_task(task_id)["status"], TASK_CANCELLED)

    def test_reference_order_is_preserved(self) -> None:
        first = self.store.save_reference(make_image(Path(self.temp.name) / "a.png", (1, 2, 3)))
        second = self.store.save_reference(make_image(Path(self.temp.name) / "b.png", (4, 5, 6)))
        task_id = self.database.create_task(
            self._request(references=(first, second))
        )
        refs = self.database.get_task_references(task_id)
        self.assertEqual([item.content_hash for item in refs], [first.content_hash, second.content_hash])
        self.assertEqual([item.original_name for item in refs], ["a.png", "b.png"])
        self.assertEqual(refs[0].source_path, first.source_path)
        self.assertEqual(refs[1].source_path, second.source_path)

    def test_library_save_search_tags_previews_and_delete(self) -> None:
        reference = self._reference()
        self._create_completed_result("带参考图作品")
        # Attach a reference to the task before saving by using a second task.
        ref_task = self.database.create_task(
            self._request(prompt="带参考图作品", references=(reference,))
        )
        self.database.claim_next_queued()
        ref_image = self.store.store_generated(ref_task, png_bytes((90, 20, 30)), 0)
        ref_thumb = self.store.create_thumbnail(ref_image.path)
        result_id = self.database.add_task_result(
            ref_task, 0, ref_image, thumbnail_path=ref_thumb, remote_url=None
        )
        self.database.transition_task(ref_task, TASK_SUCCESS)
        work_one = self.library.save_task_result(ref_task, result_id)
        self.database.set_work_tags(work_one, ["人像", "春天"])

        task_two, result_two, _ = self._create_completed_result("商业海报", (70, 100, 10))
        work_two = self.library.save_task_result(task_two, result_two)
        self.database.set_work_tags(work_two, ["海报"])

        self.assertEqual(len(self.database.list_works()), 2)
        self.assertEqual(len(self.database.list_works(search="春天")), 1)
        self.assertEqual(
            [work["id"] for work in self.database.list_works(tags=["人像", "春天"])],
            [work_one],
        )
        work_rows = self.database.list_works()
        self.assertTrue(all(Path(row["cover_path"]).name == "thumbnail.jpg" for row in work_rows))
        previews = self.database.list_work_result_previews()
        self.assertEqual(len(previews), 2)
        self.assertTrue(all(Path(row["preview_path"]).name == "thumbnail.jpg" for row in previews))
        assets = self.database.get_work_assets(work_one)
        self.assertEqual(len([asset for asset in assets if asset["role"] == "reference"]), 1)
        self.assertEqual(len([asset for asset in assets if asset["role"] == "result"]), 1)
        self.assertEqual(len([asset for asset in assets if asset["role"] == "thumbnail"]), 1)
        saved_result = self.database.get_task_results(ref_task)[0]
        self.assertTrue(saved_result["saved"])
        self.assertIn(str(self.paths.library), saved_result["path"])
        folder = self.paths.library / self.database.get_work(work_one)["folder_name"]
        self.assertTrue(folder.is_dir())
        self.assertTrue(self.library.delete_work(work_one))
        self.assertFalse(folder.exists())
        self.assertIsNone(self.database.get_work(work_one))

    def test_templates_crud(self) -> None:
        template_id = self.database.save_template("人像", "柔和光线的人像")
        custom = next(
            item for item in self.database.list_templates() if item["name"] == "人像"
        )
        self.assertEqual(custom["prompt"], "柔和光线的人像")
        self.database.save_template("人像", "更新后的提示词")
        custom = next(
            item for item in self.database.list_templates() if item["name"] == "人像"
        )
        self.assertEqual(custom["prompt"], "更新后的提示词")
        self.database.delete_template(template_id)
        self.assertNotIn(
            "人像",
            [item["name"] for item in self.database.list_templates()],
        )

    def test_builtin_templates_are_seeded_once_and_remain_editable(self) -> None:
        templates = {
            item["name"]: item for item in self.database.list_templates()
        }
        self.assertEqual(
            set(templates),
            {name for name, _ in BUILTIN_PROMPT_TEMPLATES},
        )
        base = templates["内置·基础服装替换"]
        self.assertIn("【图2】中的人物的上衣改为【图1】中的服装", base["prompt"])

        self.database.save_template("内置·基础服装替换", "用户修改后的内容")
        self.database.delete_template(templates["内置·多图换装"]["id"])
        self.database.initialize()
        reopened = {
            item["name"]: item["prompt"]
            for item in self.database.list_templates()
        }
        self.assertEqual(reopened["内置·基础服装替换"], "用户修改后的内容")
        self.assertNotIn("内置·多图换装", reopened)

    def test_manual_images_become_independent_library_works(self) -> None:
        first = make_image(
            Path(self.temp.name) / "手动 第一张.png",
            (20, 40, 80),
        )
        second = make_image(
            Path(self.temp.name) / "manual-second.png",
            (80, 40, 20),
        )
        summary = self.library.import_images([first, second, first])
        self.assertEqual(len(summary.work_ids), 3)
        self.assertEqual(summary.errors, ())

        works = self.database.list_works()
        self.assertEqual(len(works), 3)
        titles = [work["title"] for work in works]
        self.assertIn("手动 第一张", titles)
        self.assertIn("manual-second", titles)
        manual = next(work for work in works if work["title"] == "manual-second")
        self.assertIsNone(manual["task_id"])
        self.assertEqual(manual["model"], "手动上传")
        self.assertEqual(manual["prompt"], "")
        parameters = self.library.work_parameters(manual)
        self.assertEqual(parameters["source"], "manual_upload")
        self.assertEqual(parameters["width"], 64)
        self.assertEqual(parameters["height"], 80)

        folder = self.library.work_folder(manual["id"])
        self.assertTrue(any(folder.glob("output.*")))
        self.assertTrue((folder / "thumbnail.jpg").is_file())
        assets = self.database.get_work_assets(manual["id"])
        self.assertEqual(
            sorted(asset["role"] for asset in assets),
            ["result", "thumbnail"],
        )

        self.database.set_work_tags(manual["id"], ["手动", "人像"])
        self.assertEqual(
            [work["id"] for work in self.database.list_works(tags=["手动", "人像"])],
            [manual["id"]],
        )
        self.assertTrue(self.library.delete_work(manual["id"]))
        self.assertFalse(folder.exists())

    def test_manual_import_keeps_successes_when_one_file_fails(self) -> None:
        valid = make_image(
            Path(self.temp.name) / "valid.png",
            (20, 70, 40),
        )
        invalid = Path(self.temp.name) / "broken.png"
        invalid.write_bytes(b"not an image")
        summary = self.library.import_images([invalid, valid])
        self.assertEqual(len(summary.work_ids), 1)
        self.assertEqual(len(summary.errors), 1)
        self.assertIn("broken.png", summary.errors[0])
        self.assertEqual(len(self.database.list_works()), 1)

    def test_data_directory_ensure_preserves_sentinel(self) -> None:
        sentinel = self.paths.root / "keep.txt"
        sentinel.write_text("保留", encoding="utf-8")
        self.paths.ensure()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "保留")
        self.assertTrue(self.paths.config.is_dir())
        self.assertTrue(self.paths.temp.is_dir())
        self.assertTrue(self.paths.library.is_dir())
        self.assertTrue(self.paths.logs.is_dir())

    def test_app_settings_persist(self) -> None:
        settings = AppSettings(self.database)
        self.assertEqual(settings.local_save_mode(), "ask")
        self.assertEqual(settings.auto_strategy(), AUTO_STRATEGY_QUALITY)
        settings.set_local_save_mode("fixed")
        settings.set_auto_strategy(AUTO_STRATEGY_VALUE)
        settings.set_fixed_folder(Path(self.temp.name) / "fixed")
        settings.set_last_folder(Path(self.temp.name) / "last")
        reopened = AppSettings(Database(self.paths.database))
        self.assertEqual(reopened.local_save_mode(), "fixed")
        self.assertEqual(reopened.auto_strategy(), AUTO_STRATEGY_VALUE)
        self.assertEqual(reopened.fixed_folder(), Path(self.temp.name) / "fixed")
        self.assertEqual(reopened.last_folder(), Path(self.temp.name) / "last")

    def test_input_folder_settings_are_independent_and_clear_missing_paths(
        self,
    ) -> None:
        settings = AppSettings(self.database)
        create_folder = Path(self.temp.name) / "create"
        garment_folder = Path(self.temp.name) / "garment"
        person_folder = Path(self.temp.name) / "person"
        manual_folder = Path(self.temp.name) / "manual"
        for folder in (
            create_folder,
            garment_folder,
            person_folder,
            manual_folder,
        ):
            folder.mkdir()
        settings.set_input_folder(INPUT_CREATE_REFERENCE, create_folder)
        settings.set_input_folder(INPUT_BATCH_GARMENT, garment_folder)
        settings.set_input_folder(INPUT_BATCH_PERSON, person_folder)
        settings.set_input_folder(INPUT_MANUAL_UPLOAD, manual_folder)
        self.assertEqual(
            settings.input_folder(INPUT_CREATE_REFERENCE),
            create_folder.resolve(),
        )
        self.assertEqual(
            settings.input_folder(INPUT_BATCH_GARMENT),
            garment_folder.resolve(),
        )
        self.assertEqual(
            settings.input_folder(INPUT_BATCH_PERSON),
            person_folder.resolve(),
        )
        self.assertEqual(
            settings.input_folder(INPUT_MANUAL_UPLOAD),
            manual_folder.resolve(),
        )
        garment_folder.rmdir()
        self.assertIsNone(settings.input_folder(INPUT_BATCH_GARMENT))
        self.assertEqual(
            self.database.get_setting("input_last_folder_batch_garment", ""),
            "",
        )
        manual_folder.rmdir()
        self.assertIsNone(settings.input_folder(INPUT_MANUAL_UPLOAD))
        self.assertEqual(
            self.database.get_setting("input_last_folder_manual_upload", ""),
            "",
        )

    def test_version_one_database_migrates_without_losing_rows(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy" / "app.db"
        legacy_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """
                CREATE TABLE task_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    original_name TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO task_assets(
                    task_id, role, position, path, content_hash, mime,
                    width, height, original_name
                ) VALUES (1, 'reference', 0, 'old.png', 'hash', 'image/png', 10, 10, 'old.png')
                """
            )
            connection.commit()
        finally:
            connection.close()
        migrated = Database(legacy_path)
        migrated.initialize()
        with migrated.connection() as db:
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(task_assets)")
            }
            count = db.execute("SELECT COUNT(*) AS count FROM task_assets").fetchone()[
                "count"
            ]
            version = db.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()["value"]
        self.assertIn("source_path", columns)
        self.assertEqual(count, 1)
        self.assertEqual(version, "3")

    def test_version_two_database_adds_auto_model_columns(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy-v2" / "app.db"
        legacy_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO tasks(status, model) VALUES ('success', 'old-model')"
            )
            connection.commit()
        finally:
            connection.close()
        migrated = Database(legacy_path)
        migrated.initialize()
        with migrated.connection() as db:
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(tasks)")
            }
            row = db.execute(
                "SELECT model, auto_strategy, auto_reason FROM tasks"
            ).fetchone()
        self.assertIn("auto_strategy", columns)
        self.assertIn("auto_reason", columns)
        self.assertEqual(row["model"], "old-model")
        self.assertIsNone(row["auto_strategy"])
        self.assertIsNone(row["auto_reason"])


if __name__ == "__main__":
    unittest.main()
