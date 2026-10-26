import importlib.util
import os
import unittest

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None

if HAS_PYSIDE:
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QDialogButtonBox,
        QMessageBox,
        QVBoxLayout,
        QWidget,
    )

    from yixin.ui.localization import (
        install_chinese_dialog_filter,
        localize_dialog,
    )


@unittest.skipUnless(HAS_PYSIDE, "PySide6 未安装")
class ChineseDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

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


if __name__ == "__main__":
    unittest.main()
