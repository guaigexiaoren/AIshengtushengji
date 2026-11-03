from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)


class PageHeader(QWidget):
    def __init__(
        self,
        title: str,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(4)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("PageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("PageSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.subtitle_label)
        layout.addLayout(text_layout, 1)
        self.extra_layout = QHBoxLayout()
        self.extra_layout.setSpacing(8)
        layout.addLayout(self.extra_layout)

    def add_action(self, widget: QWidget) -> None:
        self.extra_layout.addWidget(widget)


class SectionCard(QFrame):
    def __init__(
        self,
        title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(18, 16, 18, 16)
        self.layout.setSpacing(12)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("SectionTitle")
        self.title_label.setVisible(bool(title))
        if title:
            self.layout.addWidget(self.title_label)


class FlowLayout(QLayout):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        horizontal_spacing: int = 8,
        vertical_spacing: int = 8,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._stretches: dict[int, int] = {}
        self._horizontal_spacing = horizontal_spacing
        self._vertical_spacing = vertical_spacing
        self.setContentsMargins(12, 10, 12, 10)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def addWidget(self, widget: QWidget, stretch: int = 0) -> None:
        self.addChildWidget(widget)
        item = QWidgetItem(widget)
        self.addItem(item)
        self._stretches[id(item)] = stretch

    def addStretch(self, stretch: int = 0) -> None:
        return

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem:
        item = self._items.pop(index)
        self._stretches.pop(id(item), None)
        return item

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return QSize(720, self.heightForWidth(720))

    def minimumSize(self) -> QSize:
        margins = self.contentsMargins()
        width = max(
            (
                item.minimumSize().width()
                for item in self._items
            ),
            default=0,
        )
        height = max(
            (
                item.minimumSize().height()
                for item in self._items
            ),
            default=0,
        )
        return QSize(
            width + margins.left() + margins.right(),
            height + margins.top() + margins.bottom(),
        )

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        content = rect.adjusted(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom(),
        )
        available_width = max(1, content.width())
        lines: list[list[QLayoutItem]] = []
        line: list[QLayoutItem] = []
        used_width = 0
        for item in self._items:
            item_width = item.sizeHint().width()
            if line and (
                used_width
                + self._horizontal_spacing
                + item_width
                > available_width
            ):
                deficit = (
                    used_width
                    + self._horizontal_spacing
                    + item_width
                    - available_width
                )
                shrinkable = sum(
                    max(
                        0,
                        existing.sizeHint().width()
                        - existing.minimumSize().width(),
                    )
                    for existing in line
                    if self._stretches.get(id(existing), 0)
                )
                if deficit <= shrinkable:
                    used_width += (
                        self._horizontal_spacing + item_width
                    )
                    line.append(item)
                    continue
                lines.append(line)
                line = [item]
                used_width = item_width
                continue
            used_width += (
                item_width
                if not line
                else self._horizontal_spacing + item_width
            )
            line.append(item)
        if line:
            lines.append(line)

        y = content.top()
        for line_items in lines:
            sizes = [item.sizeHint() for item in line_items]
            total_width = sum(size.width() for size in sizes)
            total_width += self._horizontal_spacing * (
                len(line_items) - 1
            )
            extra_width = max(0, available_width - total_width)
            stretch_total = sum(
                self._stretches.get(id(item), 0)
                for item in line_items
            )
            deficit = max(0, total_width - available_width)
            widths = [size.width() for size in sizes]
            if deficit and stretch_total:
                shrink_weights = [
                    max(
                        0,
                        size.width() - item.minimumSize().width(),
                    )
                    * self._stretches.get(id(item), 0)
                    for item, size in zip(line_items, sizes)
                ]
                shrinkable_total = sum(shrink_weights)
                reduction = min(deficit, shrinkable_total)
                for index, (item, size) in enumerate(
                    zip(line_items, sizes)
                ):
                    weight = shrink_weights[index]
                    if not weight:
                        continue
                    widths[index] -= int(
                        reduction * weight / max(1, shrinkable_total)
                    )
            x = content.left()
            line_height = max(size.height() for size in sizes)
            for item, size, item_width in zip(
                line_items,
                sizes,
                widths,
            ):
                stretch = self._stretches.get(id(item), 0)
                if extra_width and stretch_total:
                    item_width += int(
                        extra_width * stretch / stretch_total
                    )
                if not test_only:
                    item.setGeometry(
                        QRect(x, y, item_width, line_height)
                    )
                x += item_width + self._horizontal_spacing
            y += line_height + self._vertical_spacing

        if lines:
            y -= self._vertical_spacing
        return y - rect.top() + margins.bottom()


class FilterBar(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FilterBar")
        self.layout = FlowLayout(self)


class PageActionBar(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PageActionBar")
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(18, 10, 18, 10)
        self.layout.setSpacing(8)


class EmptyState(QLabel):
    def __init__(
        self,
        text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setObjectName("EmptyState")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)


class StatusBadge(QLabel):
    def __init__(
        self,
        text: str,
        tone: str = "neutral",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setObjectName("StatusBadge")
        self.setProperty("tone", tone)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
