from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import (
    QCalendarWidget,
    QDialog,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class OptionalDateEdit(QPushButton):
    dateChanged = Signal(object)

    def __init__(
        self,
        placeholder: str = "不限",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.placeholder = placeholder
        self._value: date | None = None
        self.setMinimumWidth(104)
        self.clicked.connect(self._choose_date)
        self._refresh_text()

    def date(self) -> QDate | None:
        return QDate(self._value) if self._value else None

    def python_date(self) -> date | None:
        return self._value

    def setDate(self, value: QDate | None) -> None:
        new_value = value.toPython() if value is not None else None
        if new_value == self._value:
            return
        self._value = new_value
        self._refresh_text()
        self.dateChanged.emit(new_value)

    def clear(self) -> None:
        self.setDate(None)

    def _refresh_text(self) -> None:
        self.setText(
            self._value.strftime("%Y-%m-%d")
            if self._value is not None
            else self.placeholder
        )

    def _choose_date(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("选择日期")
        dialog.resize(420, 360)
        layout = QVBoxLayout(dialog)
        calendar = QCalendarWidget()
        initial = self._value or date.today()
        calendar.setCurrentPage(initial.year, initial.month)
        calendar.setSelectedDate(QDate(initial))
        layout.addWidget(calendar, 1)
        actions = QHBoxLayout()
        clear = QPushButton("不限")
        cancel = QPushButton("取消")
        accept = QPushButton("确定")
        accept.setObjectName("PrimaryButton")
        cleared = False

        def clear_value() -> None:
            nonlocal cleared
            cleared = True
            self.clear()
            dialog.accept()

        clear.clicked.connect(clear_value)
        cancel.clicked.connect(dialog.reject)
        accept.clicked.connect(dialog.accept)
        actions.addWidget(clear)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(accept)
        layout.addLayout(actions)
        if (
            dialog.exec() == QDialog.DialogCode.Accepted
            and not cleared
        ):
            selected = calendar.selectedDate()
            if selected.isValid():
                self.setDate(selected)
