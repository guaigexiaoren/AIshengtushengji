import importlib.util
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from PIL import Image

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None

if HAS_PYSIDE:
    from PySide6.QtCore import QSize
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from tests.test_collaboration_pages import FakeCentralRepository
    from yixin.collaboration.shared_paths import SharedPathResolver
    from yixin.collaboration.types import ROLE_ADMIN, AuthUser
    from yixin.ui.async_thumbnails import ThumbnailLoader
    from yixin.ui.task_create_page import TaskCreatePage


@unittest.skipUnless(HAS_PYSIDE, "PySide6 未安装")
class AsyncThumbnailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_loader_caches_and_serves_same_source_to_multiple_requests(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_path = root / "source.png"
            Image.new("RGB", (800, 600), "red").save(image_path)
            loader = ThumbnailLoader()
            ready = []
            loader.thumbnail_ready.connect(
                lambda request_id, path, image: ready.append(
                    (request_id, path, image)
                )
            )
            first = loader.request_thumbnail(image_path, QSize(56, 70))
            second = loader.request_thumbnail(image_path, QSize(96, 96))
            for _ in range(100):
                self.app.processEvents()
                if len(ready) == 2:
                    break
                QTest.qWait(10)
            self.assertEqual(len(ready), 2)
            self.assertEqual(
                {ready[0][0], ready[1][0]},
                {first.request_id, second.request_id},
            )
            self.assertEqual(loader.cache_size(), 1)
            loader.clear_cache()

    def test_new_task_page_shows_items_before_thumbnails_finish(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            today = date.today()
            product = (
                root
                / "内部项目"
                / "春季项目"
                / f"{today.month}.{today.day}"
                / "BENCH"
            )
            (product / "衣服图").mkdir(parents=True)
            (product / "模特图").mkdir(parents=True)
            for index in range(13):
                Image.new("RGB", (300, 400), "red").save(
                    product / "衣服图" / f"衣服{index + 1}.png"
                )
            for index in range(20):
                Image.new("RGB", (300, 400), "blue").save(
                    product / "模特图" / f"模特{index + 1}.png"
                )
            page = TaskCreatePage(
                FakeCentralRepository(),
                SharedPathResolver(root),
                AuthUser(
                    1,
                    "admin",
                    "系统管理员",
                    ROLE_ADMIN,
                ),
            )
            self.assertEqual(page.garment_list.count(), 13)
            self.assertEqual(page.model_list.count(), 20)
            for _ in range(200):
                self.app.processEvents()
                loaded = (
                    all(
                        not page.garment_list.item(index).icon().isNull()
                        for index in range(page.garment_list.count())
                    )
                    and all(
                        not page.model_list.item(index).icon().isNull()
                        for index in range(page.model_list.count())
                    )
                )
                if loaded:
                    break
                QTest.qWait(10)
            self.assertTrue(loaded)
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
