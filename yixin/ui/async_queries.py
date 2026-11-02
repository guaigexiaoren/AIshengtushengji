from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QRunnable,
    QThreadPool,
    Signal,
)


class _QuerySignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class _QueryTask(QRunnable):
    def __init__(self, request_id: int, query: Callable[[], Any]) -> None:
        super().__init__()
        self.request_id = request_id
        self.query = query
        self.signals = _QuerySignals()

    def run(self) -> None:
        try:
            result = self.query()
        except Exception as exc:
            self.signals.failed.emit(self.request_id, str(exc))
            return
        self.signals.succeeded.emit(self.request_id, result)


class AsyncQueryRunner(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._next_request_id = 1

    def submit(self, query: Callable[[], Any]) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        task = _QueryTask(request_id, query)
        task.signals.succeeded.connect(self.succeeded)
        task.signals.failed.connect(self.failed)
        self._pool.start(task)
        return request_id


_query_runner: AsyncQueryRunner | None = None


def get_query_runner() -> AsyncQueryRunner:
    global _query_runner
    if _query_runner is None:
        _query_runner = AsyncQueryRunner(
            QCoreApplication.instance()
        )
    return _query_runner
