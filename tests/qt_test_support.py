from __future__ import annotations

import os


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def flush_qt_deletes(app=None) -> None:
    """关闭窗口并刷新 Qt 事件，避免退出时重复清理对象。"""
    from PySide6.QtWidgets import QApplication

    application = app or QApplication.instance()
    if application is None:
        return

    QApplication.closeAllWindows()
    application.processEvents()
