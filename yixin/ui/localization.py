from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QMessageBox,
)


BUTTON_TEXT = {
    QDialogButtonBox.StandardButton.Ok: "确定",
    QDialogButtonBox.StandardButton.Save: "保存",
    QDialogButtonBox.StandardButton.Cancel: "取消",
    QDialogButtonBox.StandardButton.Close: "关闭",
    QDialogButtonBox.StandardButton.Open: "打开",
    QDialogButtonBox.StandardButton.Apply: "应用",
    QDialogButtonBox.StandardButton.Reset: "重置",
    QDialogButtonBox.StandardButton.Discard: "放弃",
    QDialogButtonBox.StandardButton.Yes: "是",
    QDialogButtonBox.StandardButton.No: "否",
}
MESSAGEBOX_BUTTON_TEXT = {
    QMessageBox.StandardButton.Ok: "确定",
    QMessageBox.StandardButton.Save: "保存",
    QMessageBox.StandardButton.Cancel: "取消",
    QMessageBox.StandardButton.Close: "关闭",
    QMessageBox.StandardButton.Open: "打开",
    QMessageBox.StandardButton.Apply: "应用",
    QMessageBox.StandardButton.Reset: "重置",
    QMessageBox.StandardButton.Discard: "放弃",
    QMessageBox.StandardButton.Yes: "是",
    QMessageBox.StandardButton.No: "否",
}


def localize_dialog(dialog: QDialog) -> None:
    for button_box in dialog.findChildren(QDialogButtonBox):
        for standard, text in BUTTON_TEXT.items():
            button = button_box.button(standard)
            if button is not None:
                button.setText(text)
    if isinstance(dialog, QMessageBox):
        for standard, text in MESSAGEBOX_BUTTON_TEXT.items():
            button = dialog.button(standard)
            if button is not None:
                button.setText(text)
    if isinstance(dialog, QInputDialog):
        dialog.setOkButtonText("确定")
        dialog.setCancelButtonText("取消")
    if isinstance(dialog, QFileDialog):
        dialog.setLabelText(QFileDialog.DialogLabel.Accept, "选择")
        dialog.setLabelText(QFileDialog.DialogLabel.Reject, "取消")
        dialog.setLabelText(QFileDialog.DialogLabel.LookIn, "位置")
        dialog.setLabelText(QFileDialog.DialogLabel.FileName, "文件名")
        dialog.setLabelText(QFileDialog.DialogLabel.FileType, "文件类型")


class ChineseDialogFilter(QObject):
    def eventFilter(self, watched, event) -> bool:
        if (
            event.type() == QEvent.Type.Show
            and isinstance(watched, QDialog)
        ):
            QTimer.singleShot(0, lambda dialog=watched: localize_dialog(dialog))
        return False


def install_chinese_dialog_filter(app: QApplication) -> ChineseDialogFilter:
    existing = getattr(app, "_chinese_dialog_filter", None)
    if existing is not None:
        return existing
    event_filter = ChineseDialogFilter(app)
    app.installEventFilter(event_filter)
    app._chinese_dialog_filter = event_filter
    return event_filter
