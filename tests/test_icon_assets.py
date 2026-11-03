from __future__ import annotations

import unittest
from pathlib import Path

from tests.qt_test_support import flush_qt_deletes

from PIL import Image
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from yixin.paths import resource_path


class IconAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.assets = Path(__file__).resolve().parents[1] / "assets"

    def test_icon_assets_exist(self) -> None:
        self.assertTrue((self.assets / "app_icon.svg").is_file())
        self.assertTrue((self.assets / "app_icon.png").is_file())
        self.assertTrue((self.assets / "app_icon.ico").is_file())
        self.assertTrue((self.assets / "icon_preview.png").is_file())

    def test_png_has_expected_size_and_transparency(self) -> None:
        with Image.open(self.assets / "app_icon.png") as image:
            self.assertEqual(image.size, (512, 512))
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertGreater(image.getpixel((256, 256))[3], 0)

    def test_ico_contains_required_sizes(self) -> None:
        with Image.open(self.assets / "app_icon.ico") as image:
            sizes = {size for size in image.ico.sizes()}
        expected = {(size, size) for size in (16, 24, 32, 48, 64, 128, 256)}
        self.assertTrue(expected.issubset(sizes))

    def test_svg_has_no_text_and_runtime_icon_loads(self) -> None:
        svg = (self.assets / "app_icon.svg").read_text(encoding="utf-8")
        self.assertNotIn("<text", svg)
        icon = QIcon(str(resource_path("assets", "app_icon.ico")))
        self.assertFalse(icon.isNull())
        self.assertIn(16, [size.width() for size in icon.availableSizes()])

    def tearDown(self) -> None:
        flush_qt_deletes(self.app)

    @classmethod
    def tearDownClass(cls) -> None:
        flush_qt_deletes(cls.app)


if __name__ == "__main__":
    unittest.main()
