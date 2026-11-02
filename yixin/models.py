from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .constants import (
    ASPECT_RATIOS,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_BATCH_SIZE,
    DEFAULT_RESOLUTION,
    MAX_BATCH_SIZE,
    MAX_REFERENCE_IMAGES,
    MODE_DRESS_UP,
    MODE_FREE_COMPOSE,
    MODE_LABELS,
    MODE_REFERENCE,
    MODE_TEXT,
    MODEL_IDS,
    RESOLUTIONS,
)


@dataclass(frozen=True)
class StoredImage:
    path: Path
    content_hash: str
    mime: str
    width: int
    height: int
    original_name: str
    source_path: Path | None = None


@dataclass(frozen=True)
class ImageRequest:
    model: str
    prompt: str
    n: int = DEFAULT_BATCH_SIZE
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    resolution: str | None = DEFAULT_RESOLUTION
    reference_images: tuple[StoredImage, ...] = ()
    mode: str = MODE_TEXT
    auto_strategy: str | None = None
    auto_reason: str | None = None
    provider_code: str | None = None
    central_attempt_id: int | None = None

    def validate(self) -> None:
        if self.model not in MODEL_IDS:
            raise ValueError("不支持的模型")
        if not self.prompt or not self.prompt.strip():
            raise ValueError("提示词不能为空")
        if not 1 <= self.n <= MAX_BATCH_SIZE:
            raise ValueError(f"生成数量必须在 1 到 {MAX_BATCH_SIZE} 之间")
        if self.aspect_ratio not in ASPECT_RATIOS:
            raise ValueError("不支持的宽高比")
        if self.resolution not in (None, "auto", *RESOLUTIONS[1:]):
            raise ValueError("不支持的分辨率")
        if self.mode not in (
            MODE_TEXT,
            MODE_REFERENCE,
            MODE_DRESS_UP,
            MODE_FREE_COMPOSE,
        ):
            raise ValueError("不支持的生成模式")
        if len(self.reference_images) > MAX_REFERENCE_IMAGES:
            raise ValueError(f"参考图最多 {MAX_REFERENCE_IMAGES} 张")
        if self.mode == MODE_DRESS_UP and not 2 <= len(self.reference_images) <= MAX_REFERENCE_IMAGES:
            raise ValueError("批量换装任务必须包含 2 到 14 张参考图")
        if self.mode == MODE_FREE_COMPOSE and not self.reference_images:
            raise ValueError("自由组合至少需要一张参考图")
        if self.mode == MODE_REFERENCE and not self.reference_images:
            raise ValueError("参考图编辑至少需要一张参考图")
        if self.mode == MODE_TEXT and self.reference_images:
            raise ValueError("文字生图模式不能附带参考图")

    @property
    def mode_label(self) -> str:
        return MODE_LABELS[self.mode]

    @property
    def effective_resolution(self) -> str | None:
        return None if self.resolution in (None, "auto") else self.resolution


@dataclass(frozen=True)
class RemoteImage:
    url: str | None = None
    b64_json: str | None = None


@dataclass(frozen=True)
class ImageApiResult:
    images: tuple[RemoteImage, ...]
    request_id: str | None
    remote_task_id: str | None
    usage_input_tokens: int | None
    usage_output_tokens: int | None
    raw_response: Mapping[str, Any]
    status_code: int
    provider_id: int | None = None
    provider_code: str = ""
    provider_name: str = ""
    provider_model: str = ""


class ApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        remote_task_id: str | None = None,
        error_code: str | None = None,
        raw_response: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        self.remote_task_id = remote_task_id
        self.error_code = error_code
        self.raw_response = dict(raw_response or {})


class ConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class WorkerTask:
    id: int
    mode: str
    model: str
    prompt: str
    aspect_ratio: str
    resolution: str | None
    n: int
    references: tuple[StoredImage, ...] = field(default_factory=tuple)
