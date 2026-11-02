from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class PaginationBar(QWidget):
    page_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.page = 1
        self.total_pages = 1
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)
        self.summary = QLabel("共 0 条")
        self.summary.setStyleSheet("color:#60708a;")
        self.first_button = QPushButton("首页")
        self.previous_button = QPushButton("上一页")
        self.page_label = QLabel("第 1 / 1 页")
        self.next_button = QPushButton("下一页")
        self.last_button = QPushButton("末页")
        layout.addWidget(self.summary)
        layout.addStretch(1)
        for button in (
            self.first_button,
            self.previous_button,
        ):
            button.setFixedWidth(64)
            layout.addWidget(button)
        layout.addWidget(self.page_label)
        for button in (
            self.next_button,
            self.last_button,
        ):
            button.setFixedWidth(64)
            layout.addWidget(button)
        self.first_button.clicked.connect(lambda: self._request(1))
        self.previous_button.clicked.connect(
            lambda: self._request(self.page - 1)
        )
        self.next_button.clicked.connect(
            lambda: self._request(self.page + 1)
        )
        self.last_button.clicked.connect(
            lambda: self._request(self.total_pages)
        )
        self.set_result(0, 1, 1)

    def set_result(
        self,
        total: int,
        page: int,
        page_size: int,
    ) -> None:
        self.page = max(1, int(page))
        total_pages = max(1, (max(0, total) + page_size - 1) // page_size)
        if self.page > total_pages:
            self.page = total_pages
        self.total_pages = total_pages
        self.summary.setText(
            f"共 {max(0, total)} 条 · 每页 {page_size} 条"
        )
        self.page_label.setText(
            f"第 {self.page} / {self.total_pages} 页"
        )
        self.first_button.setEnabled(self.page > 1)
        self.previous_button.setEnabled(self.page > 1)
        self.next_button.setEnabled(self.page < self.total_pages)
        self.last_button.setEnabled(self.page < self.total_pages)

    def _request(self, page: int) -> None:
        page = max(1, min(page, self.total_pages))
        if page != self.page:
            self.page_requested.emit(page)
