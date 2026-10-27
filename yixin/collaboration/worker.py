from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ..api import NanoBananaClient
from ..logging_utils import sanitize_text
from ..models import ApiError, ImageRequest, StoredImage
from ..storage import (
    MIME_TO_SUFFIX,
    FileStore,
    atomic_write_bytes,
    image_metadata,
    sha256_bytes,
)
from .repository import MySQLCentralRepository
from .shared_paths import SharedPathResolver
from .types import (
    AuthUser,
    GeneratedResultInput,
    GenerationAttempt,
    ProjectTask,
)


class GenerationWorkerSignals(QObject):
    finished = Signal(int, int)
    failed = Signal(int, str, str)


class ProjectGenerationWorker:
    def __init__(
        self,
        *,
        repository: MySQLCentralRepository,
        client: NanoBananaClient,
        file_store: FileStore,
        resolver: SharedPathResolver,
        task: ProjectTask,
        actor: AuthUser,
        prompt: str,
        model: str,
        aspect_ratio: str,
        resolution: str | None,
        n: int,
        local_references: tuple[StoredImage, ...] | None = None,
        reserved_attempt: GenerationAttempt | None = None,
    ) -> None:
        self.repository = repository
        self.client = client
        self.file_store = file_store
        self.resolver = resolver
        self.task = task
        self.actor = actor
        self.prompt = prompt
        self.model = model
        self.aspect_ratio = aspect_ratio
        self.resolution = resolution
        self.n = n
        self.local_references = local_references
        self.reserved_attempt = reserved_attempt
        self.signals = GenerationWorkerSignals()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"project-attempt-{self.task.id}",
            daemon=True,
        )
        self._thread.start()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        attempt_id = 0
        try:
            current_task = self.repository.get_task(self.task.id)
            if current_task is None:
                raise ValueError("子任务不存在")
            self.task = current_task
            unit_price = self.repository.current_model_price(self.model)
            if self.reserved_attempt is not None:
                attempt_id = self.reserved_attempt.id
                attempt = self.repository.start_queued_generation_attempt(
                    attempt_id,
                    self.actor,
                )
            else:
                attempt = self.repository.create_generation_attempt(
                    self.task.id,
                    self.actor,
                    prompt=self.prompt,
                    model=self.model,
                    aspect_ratio=self.aspect_ratio,
                    resolution=self.resolution,
                    n=self.n,
                    unit_price=unit_price,
                )
            attempt_id = attempt.id
            project_root = self.resolver.project_root(
                self.task.project_relative_root
            )
            if self.local_references is not None:
                references = self.local_references
                if not 2 <= len(references) <= 14:
                    raise ValueError(
                        "本机素材必须至少包含一张衣服图和一张模特图"
                    )
            else:
                garment_assets = self.task.garment_assets
                if len(garment_assets) > 13:
                    raise ValueError("每个子任务最多支持 13 张衣服图和 1 张模特图")
                references = tuple(
                    self._stored_reference(
                        asset.relative_path,
                        asset.original_name,
                        asset.content_hash,
                    )
                    for asset in garment_assets
                ) + (
                    self._stored_reference(
                        self.task.model_path,
                        self.task.model_name,
                        self.task.model_hash,
                    ),
                )
            request = ImageRequest(
                model=self.model,
                prompt=self.prompt,
                n=self.n,
                aspect_ratio=self.aspect_ratio,
                resolution=self.resolution,
                reference_images=references,
                mode="dress_up",
            )
            api_result = self.client.generate(request)
            output_directory = self.resolver.output_directory(
                self.task,
                attempt.attempt_no,
            )
            output_directory.mkdir(parents=True, exist_ok=True)
            generated: list[GeneratedResultInput] = []
            for index, remote in enumerate(api_result.images):
                data = self.client.resolve_image(remote)
                mime, width, height = image_metadata(data)
                suffix = MIME_TO_SUFFIX.get(mime, ".png")
                content_hash = sha256_bytes(data)
                image_path = output_directory / (
                    f"result_{index + 1:02d}_{content_hash[:12]}{suffix}"
                )
                atomic_write_bytes(image_path, data)
                thumbnail_path = self.file_store.create_thumbnail(
                    image_path,
                    output_directory / f"thumb_{index + 1:02d}.jpg",
                )
                generated.append(
                    GeneratedResultInput(
                        result_index=index,
                        relative_path=self.resolver.relative_to_project(
                            project_root,
                            image_path,
                        ),
                        thumbnail_relative_path=self.resolver.relative_to_project(
                            project_root,
                            thumbnail_path,
                        ),
                        content_hash=content_hash,
                        mime=mime,
                        width=width,
                        height=height,
                    )
                )
            self.repository.complete_generation_attempt(
                attempt_id,
                self.actor,
                request_id=api_result.request_id or "",
                remote_task_id=api_result.remote_task_id or "",
                raw_response=dict(api_result.raw_response),
                input_tokens=api_result.usage_input_tokens,
                output_tokens=api_result.usage_output_tokens,
                results=generated,
            )
            self.signals.finished.emit(self.task.id, attempt_id)
        except Exception as exc:
            message = sanitize_text(str(exc))
            if attempt_id:
                try:
                    self.repository.fail_generation_attempt(
                        attempt_id,
                        self.actor,
                        error_code=(
                            exc.error_code
                            if isinstance(exc, ApiError) and exc.error_code
                            else "generation_failed"
                        ),
                        error_message=message,
                    )
                except Exception:
                    pass
            self.signals.failed.emit(self.task.id, message, str(attempt_id))

    def _stored_reference(
        self,
        relative_path: str,
        original_name: str,
        expected_hash: str,
    ) -> StoredImage:
        path = self.resolver.resolve_project_path(
            self.task.project_relative_root,
            relative_path,
        )
        if not path.is_file():
            raise FileNotFoundError(f"共享盘图片不存在：{path}")
        data = path.read_bytes()
        content_hash = sha256_bytes(data)
        if content_hash != expected_hash:
            raise ValueError(
                f"共享盘图片“{original_name}”内容已变化，请重新提交项目"
            )
        mime, width, height = image_metadata(data)
        return StoredImage(
            path=path,
            content_hash=content_hash,
            mime=mime,
            width=width,
            height=height,
            original_name=original_name,
            source_path=path,
        )
