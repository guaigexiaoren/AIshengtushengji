from __future__ import annotations

import json
import logging
import threading
import traceback

from PySide6.QtCore import QObject, Signal

from .api import NanoBananaClient
from .constants import TASK_CANCELLED, TASK_FAILED, TASK_SUCCESS
from .database import Database, StateTransitionError
from .logging_utils import sanitize_text
from .models import ApiError, ConfigurationError, ImageRequest
from .storage import FileStore


class WorkerSignals(QObject):
    task_changed = Signal(int, str)
    idle = Signal()


class TaskWorker:
    def __init__(
        self,
        database: Database,
        client: NanoBananaClient,
        file_store: FileStore,
        *,
        signals: WorkerSignals | None = None,
    ) -> None:
        self.database = database
        self.client = client
        self.file_store = file_store
        self.signals = signals or WorkerSignals()
        self.logger = logging.getLogger("yixin.worker")
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="generation-queue",
            daemon=True,
        )
        self._thread.start()
        self.notify()

    def notify(self) -> None:
        self._wake.set()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()

    def request_cancel(self, task_id: int) -> str:
        return self.database.request_cancel(task_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self.database.claim_next_queued()
            except Exception:
                self.logger.error("读取任务队列失败：%s", sanitize_text(traceback.format_exc()))
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            if task is None:
                self.signals.idle.emit()
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                self.process_task(dict(task))
            except Exception:
                # 单条任务异常不应让队列线程退出。
                self.logger.error(
                    "处理任务异常：%s",
                    sanitize_text(traceback.format_exc()),
                )

    def process_task(self, task: dict) -> None:
        task_id = int(task["id"])
        self.signals.task_changed.emit(task_id, "running")
        try:
            references = tuple(self.database.get_task_references(task_id))
            request = ImageRequest(
                model=task["model"],
                prompt=task["prompt"],
                n=int(task["n"]),
                aspect_ratio=task["aspect_ratio"],
                resolution=task["resolution"],
                reference_images=references,
                mode=task["mode"],
            )
            request.validate()
            result = self.client.generate(request)
            self.database.update_task_from_api(
                task_id,
                request_id=result.request_id,
                remote_task_id=result.remote_task_id,
                raw_response=json.dumps(
                    result.raw_response, ensure_ascii=False, sort_keys=True
                ),
                input_tokens=result.usage_input_tokens,
                output_tokens=result.usage_output_tokens,
            )
            if self.database.is_cancel_requested(task_id):
                self.database.transition_task(task_id, TASK_CANCELLED)
                self.signals.task_changed.emit(task_id, TASK_CANCELLED)
                return

            for index, remote in enumerate(result.images):
                data = self.client.resolve_image(remote)
                stored = self.file_store.store_generated(task_id, data, index)
                thumbnail = self.file_store.create_thumbnail(stored.path)
                self.database.add_task_result(
                    task_id,
                    index,
                    stored,
                    thumbnail_path=thumbnail,
                    remote_url=sanitize_text(remote.url) if remote.url else None,
                )
                if self.database.is_cancel_requested(task_id):
                    break

            if self.database.is_cancel_requested(task_id):
                self.database.transition_task(task_id, TASK_CANCELLED)
                self.signals.task_changed.emit(task_id, TASK_CANCELLED)
            else:
                self.database.transition_task(task_id, TASK_SUCCESS)
                self.signals.task_changed.emit(task_id, TASK_SUCCESS)
        except ApiError as exc:
            self.database.record_failure_metadata(
                task_id,
                request_id=exc.request_id,
                remote_task_id=exc.remote_task_id,
                raw_response=(
                    json.dumps(exc.raw_response, ensure_ascii=False, sort_keys=True)
                    if exc.raw_response
                    else None
                ),
            )
            self._finish_failure(task_id, exc.message, exc.error_code)
        except ConfigurationError as exc:
            self._finish_failure(task_id, str(exc), "configuration")
        except Exception as exc:
            self.logger.error(
                "任务 %s 执行失败：%s",
                task_id,
                sanitize_text(traceback.format_exc()),
            )
            self._finish_failure(task_id, sanitize_text(str(exc)), "internal")

    def _finish_failure(self, task_id: int, message: str, code: str | None) -> None:
        try:
            if self.database.is_cancel_requested(task_id):
                self.database.transition_task(
                    task_id, TASK_CANCELLED, error_message=message
                )
                self.signals.task_changed.emit(task_id, TASK_CANCELLED)
            else:
                self.database.transition_task(
                    task_id,
                    TASK_FAILED,
                    error_code=code,
                    error_message=sanitize_text(message),
                )
                self.signals.task_changed.emit(task_id, TASK_FAILED)
        except StateTransitionError:
            self.logger.error("任务 %s 的失败状态无法写入", task_id)
