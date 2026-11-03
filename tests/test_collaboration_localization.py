import importlib.util
import unittest
from pathlib import Path

from tests.qt_test_support import flush_qt_deletes

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None

if HAS_PYSIDE:
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QDialogButtonBox,
        QLineEdit,
        QMessageBox,
        QVBoxLayout,
        QWidget,
    )

    from yixin.ui.localization import (
        install_chinese_dialog_filter,
        install_chinese_translations,
        localize_dialog,
    )


@unittest.skipUnless(HAS_PYSIDE, "PySide6 未安装")
class ChineseDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        flush_qt_deletes(self.app)

    @classmethod
    def tearDownClass(cls) -> None:
        flush_qt_deletes(cls.app)

    def test_standard_buttons_are_chinese(self) -> None:
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        layout.addWidget(QWidget())
        layout.addWidget(buttons)
        localize_dialog(dialog)
        self.assertEqual(
            buttons.button(QDialogButtonBox.StandardButton.Save).text(),
            "保存",
        )
        self.assertEqual(
            buttons.button(QDialogButtonBox.StandardButton.Cancel).text(),
            "取消",
        )
        self.assertEqual(
            buttons.button(QDialogButtonBox.StandardButton.Ok).text(),
            "确定",
        )
        dialog.deleteLater()

    def test_message_box_yes_no_are_chinese(self) -> None:
        box = QMessageBox()
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
        )
        localize_dialog(box)
        self.assertEqual(
            box.button(QMessageBox.StandardButton.Yes).text(),
            "是",
        )
        self.assertEqual(
            box.button(QMessageBox.StandardButton.No).text(),
            "否",
        )
        box.deleteLater()

    def test_installed_filter_localizes_shown_dialogs(self) -> None:
        install_chinese_dialog_filter(self.app)
        box = QMessageBox()
        box.setText("测试")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
        )
        box.show()
        self.app.processEvents()
        self.app.processEvents()
        self.assertEqual(box.button(QMessageBox.StandardButton.Yes).text(), "是")
        self.assertEqual(box.button(QMessageBox.StandardButton.No).text(), "否")
        box.close()
        box.deleteLater()

    def test_standard_context_menu_is_chinese(self) -> None:
        translator = install_chinese_translations(self.app)
        self.assertIsNotNone(translator)
        line = QLineEdit("测试")
        menu = line.createStandardContextMenu()
        texts = [
            action.text()
            for action in menu.actions()
            if not action.isSeparator()
        ]
        self.assertTrue(any("复制" in text for text in texts))
        self.assertTrue(any("粘贴" in text for text in texts))
        self.assertTrue(any("全选" in text for text in texts))
        self.assertFalse(any("Copy" in text for text in texts))
        self.assertFalse(any("Paste" in text for text in texts))
        self.assertFalse(any("Select All" in text for text in texts))
        menu.deleteLater()
        line.deleteLater()

    def test_packaging_includes_qtbase_translation(self) -> None:
        spec = (
            Path(__file__).resolve().parent.parent
            / "亦欣AI生图工作台.spec"
        ).read_text(encoding="utf-8")
        self.assertIn('"qtbase_zh_CN.qm"', spec)
        self.assertIn('"assets/translations"', spec)


if __name__ == "__main__":
    unittest.main()
