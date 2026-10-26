from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from .constants import (
    API_BASE_URL,
    CONNECT_TIMEOUT_SECONDS,
    DOWNLOAD_ATTEMPTS,
    GENERATION_ENDPOINT,
    REQUEST_TIMEOUT_SECONDS,
)
from .logging_utils import redact_data, register_sensitive_value, sanitize_text
from .models import (
    ApiError,
    ConfigurationError,
    ImageApiResult,
    ImageRequest,
    RemoteImage,
)
from .storage import data_url_for_file


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        REQUEST_TIMEOUT_SECONDS,
        connect=CONNECT_TIMEOUT_SECONDS,
        read=REQUEST_TIMEOUT_SECONDS,
        write=REQUEST_TIMEOUT_SECONDS,
        pool=CONNECT_TIMEOUT_SECONDS,
    )


def _header(response: httpx.Response, name: str) -> str | None:
    return response.headers.get(name)


def _deep_find(payload: Any, candidate_keys: set[str]) -> Any:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() in candidate_keys and value not in (None, "", 0):
                return value
        for value in payload.values():
            found = _deep_find(value, candidate_keys)
            if found not in (None, "", 0):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _deep_find(value, candidate_keys)
            if found not in (None, "", 0):
                return found
    return None


def _extract_usage(payload: Mapping[str, Any]) -> tuple[int | None, int | None]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None, None
    input_value = usage.get("input_tokens")
    output_value = usage.get("output_tokens")
    return (
        int(input_value) if isinstance(input_value, (int, float)) else None,
        int(output_value) if isinstance(output_value, (int, float)) else None,
    )


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            return sanitize_text(error.get("message") or error.get("detail") or fallback)
        if isinstance(error, str):
            return sanitize_text(error)
        return sanitize_text(payload.get("message") or payload.get("detail") or fallback)
    return sanitize_text(fallback)


def _parse_image_items(payload: Mapping[str, Any]) -> tuple[RemoteImage, ...]:
    data = payload.get("data")
    if isinstance(data, Mapping):
        data = [data]
    if not isinstance(data, list):
        return ()
    images: list[RemoteImage] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        b64_value = item.get("b64_json")
        if isinstance(b64_value, str):
            if b64_value.startswith("data:") and "," in b64_value:
                b64_value = b64_value.split(",", 1)[1]
            images.append(RemoteImage(url=url if isinstance(url, str) else None, b64_json=b64_value))
        elif isinstance(url, str) and url:
            images.append(RemoteImage(url=url))
    return tuple(images)


class NanoBananaClient:
    def __init__(
        self,
        key_provider: Callable[[], str | None],
        *,
        api_root: str = API_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        download_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.key_provider = key_provider
        self.api_root = api_root.rstrip("/")
        self.transport = transport
        self.download_transport = download_transport

    def build_payload(self, request: ImageRequest) -> dict[str, Any]:
        request.validate()
        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": request.prompt.strip(),
            "n": request.n,
            "aspect_ratio": request.aspect_ratio,
        }
        if request.effective_resolution:
            payload["resolution"] = request.effective_resolution
        references = [
            data_url_for_file(image.path, image.mime)
            for image in request.reference_images
        ]
        if len(references) == 1:
            payload["image"] = references[0]
        elif len(references) > 1:
            payload["images"] = references
        return payload

    def generate(self, request: ImageRequest) -> ImageApiResult:
        key = self.key_provider()
        if not key:
            raise ConfigurationError("尚未配置 API Key，请联系管理员")
        register_sensitive_value(key)
        payload = self.build_payload(request)
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
        try:
            with httpx.Client(
                timeout=_timeout(),
                transport=self.transport,
                follow_redirects=True,
            ) as client:
                response = client.post(
                    f"{self.api_root}{GENERATION_ENDPOINT}",
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ApiError(
                "请求超时，任务可能仍在服务端处理并产生费用",
                error_code="timeout",
            ) from exc
        except httpx.NetworkError as exc:
            raise ApiError("网络连接失败，请检查网络后手动重试", error_code="network") from exc
        except httpx.HTTPError as exc:
            raise ApiError("请求未能完成，请手动重试", error_code="http_client") from exc

        request_id = _header(response, "x-request-id")
        try:
            raw_payload: Any = response.json()
        except (json.JSONDecodeError, ValueError):
            raw_payload = None
        if not isinstance(raw_payload, Mapping):
            snippet = sanitize_text(response.text[:2000] if response.text else "响应不是合法 JSON")
            if response.is_success:
                raise ApiError(
                    "服务端返回了无法解析的 JSON",
                    status_code=response.status_code,
                    request_id=request_id,
                    error_code="invalid_json",
                    raw_response={"body": snippet},
                )
            raise ApiError(
                snippet or f"HTTP {response.status_code}",
                status_code=response.status_code,
                request_id=request_id,
                error_code="http_error",
                raw_response={"body": snippet},
            )

        sanitized_payload = redact_data(raw_payload)
        remote_task_id = _deep_find(raw_payload, {"task_id", "taskid"})
        if remote_task_id is None:
            top_id = raw_payload.get("id")
            if isinstance(top_id, str):
                remote_task_id = top_id

        if not response.is_success:
            message = _error_message(raw_payload, f"HTTP {response.status_code}")
            if response.status_code == 401:
                message = "API Key 无效或不可用，请联系管理员"
            elif response.status_code == 429:
                message = "请求过于频繁或额度不足，请稍后手动重试"
            raise ApiError(
                message,
                status_code=response.status_code,
                request_id=request_id,
                remote_task_id=str(remote_task_id) if remote_task_id else None,
                error_code=f"http_{response.status_code}",
                raw_response=sanitized_payload,
            )

        images = _parse_image_items(raw_payload)
        if not images:
            raise ApiError(
                "服务端响应中没有图片数据",
                status_code=response.status_code,
                request_id=request_id,
                remote_task_id=str(remote_task_id) if remote_task_id else None,
                error_code="missing_images",
                raw_response=sanitized_payload,
            )
        input_tokens, output_tokens = _extract_usage(raw_payload)
        return ImageApiResult(
            images=images,
            request_id=request_id,
            remote_task_id=str(remote_task_id) if remote_task_id else None,
            usage_input_tokens=input_tokens,
            usage_output_tokens=output_tokens,
            raw_response=sanitized_payload,
            status_code=response.status_code,
        )

    def resolve_image(self, image: RemoteImage, sleep: Callable[[float], None] = time.sleep) -> bytes:
        if image.b64_json:
            try:
                compact = "".join(image.b64_json.split())
                return base64.b64decode(compact, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ApiError("服务端返回的 base64 图片无效", error_code="invalid_base64") from exc
        if not image.url:
            raise ApiError("图片结果缺少 url 或 b64_json", error_code="missing_image_field")
        last_error: Exception | None = None
        for attempt in range(DOWNLOAD_ATTEMPTS):
            try:
                with httpx.Client(
                    timeout=_timeout(),
                    transport=self.download_transport,
                    follow_redirects=True,
                ) as client:
                    response = client.get(image.url)
                if response.is_success:
                    return response.content
                if response.status_code < 500 and response.status_code != 429:
                    raise ApiError(
                        f"图片下载失败（HTTP {response.status_code}）",
                        status_code=response.status_code,
                        request_id=_header(response, "x-request-id"),
                        error_code="download_http_error",
                    )
                last_error = ApiError(
                    f"图片下载失败（HTTP {response.status_code}）",
                    status_code=response.status_code,
                    request_id=_header(response, "x-request-id"),
                    error_code="download_http_error",
                )
            except ApiError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
                last_error = exc
            if attempt < DOWNLOAD_ATTEMPTS - 1:
                sleep(0.4 * (attempt + 1))
        if isinstance(last_error, ApiError):
            raise last_error
        raise ApiError("图片下载超时或网络中断，已重试两次", error_code="download_failed") from last_error
