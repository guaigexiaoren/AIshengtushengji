from __future__ import annotations

from dataclasses import replace

import httpx

from .api import (
    NanoBananaClient,
    OpenAIChatImageClient,
    OpenAIImagesClient,
)
from .collaboration.api_keys import DatabaseApiKeyStore
from .collaboration.repository import MySQLCentralRepository
from .collaboration.types import AuthUser, ProviderModelRoute
from .constants import (
    PROTOCOL_LEGACY_JSON_IMAGES,
    PROTOCOL_OPENAI_CHAT_IMAGE,
    PROTOCOL_OPENAI_IMAGES,
)
from .models import (
    ApiError,
    ConfigurationError,
    ImageApiResult,
    ImageRequest,
    RemoteImage,
)


class ProviderRouter:
    """根据后台平台配置选择生图渠道，并在安全错误上自动切换。"""

    def __init__(
        self,
        repository: MySQLCentralRepository,
        key_store: DatabaseApiKeyStore,
        actor: AuthUser,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.repository = repository
        self.key_store = key_store
        self.actor = actor
        self.transport = transport

    def generate(self, request: ImageRequest) -> ImageApiResult:
        routes = self.repository.list_provider_routes(request.model)
        if request.provider_code:
            preferred = [
                route
                for route in routes
                if route.provider_code == request.provider_code
            ]
            routes = preferred + [
                route
                for route in routes
                if route.provider_code != request.provider_code
            ]
        if not routes:
            if request.provider_code:
                raise ConfigurationError("所选 API 平台没有配置当前模型")
            raise ConfigurationError("当前模型没有可用的 API 平台，请联系管理员")

        last_error: ApiError | None = None
        missing_key_count = 0
        attempted_count = 0
        for route in routes:
            if len(request.reference_images) > route.max_reference_images:
                continue
            if (
                route.protocol != PROTOCOL_OPENAI_CHAT_IMAGE
                and request.n > route.images_per_request
            ):
                continue
            attempted_count += 1
            try:
                key = self.key_store.load(
                    provider_id=route.provider_id,
                    strict=True,
                )
            except Exception as exc:
                missing_key_count += 1
                self._record_call(
                    request,
                    route.provider_id,
                    route.provider_name,
                    route.provider_model,
                    status="skipped",
                    error_code="api_key_unavailable",
                    error_message=str(exc),
                )
                continue
            if not key:
                missing_key_count += 1
                self._record_call(
                    request,
                    route.provider_id,
                    route.provider_name,
                    route.provider_model,
                    status="skipped",
                    error_code="api_key_unavailable",
                    error_message="尚未配置 API Key",
                )
                continue
            client = self._client_for_route(route, key)
            try:
                result = client.generate(request)
            except ApiError as exc:
                self._record_call(
                    request,
                    route.provider_id,
                    route.provider_name,
                    route.provider_model,
                    status="failed",
                    request_id=exc.request_id or "",
                    remote_task_id=exc.remote_task_id or "",
                    status_code=exc.status_code,
                    error_code=exc.error_code or "api_error",
                    error_message=exc.message,
                )
                last_error = ApiError(
                    f"{route.provider_name}：{exc.message}",
                    status_code=exc.status_code,
                    request_id=exc.request_id,
                    remote_task_id=exc.remote_task_id,
                    error_code=exc.error_code,
                    raw_response=exc.raw_response,
                )
                if not self._can_failover(exc):
                    raise last_error
                continue
            self._record_call(
                request,
                route.provider_id,
                route.provider_name,
                route.provider_model,
                status="succeeded",
                request_id=result.request_id or "",
                remote_task_id=result.remote_task_id or "",
                status_code=result.status_code,
            )
            return replace(
                result,
                provider_id=route.provider_id,
                provider_code=route.provider_code,
                provider_name=route.provider_name,
                provider_model=route.provider_model,
            )

        if not attempted_count:
            raise ConfigurationError(
                f"当前模型没有支持 {len(request.reference_images)} 张参考图的 API 平台"
            )
        if missing_key_count == attempted_count and last_error is None:
            raise ConfigurationError("当前模型未配置任何可用的 API Key，请联系管理员")
        if last_error is not None:
            raise last_error
        raise ConfigurationError("没有可用的 API 平台，请联系管理员")

    def resolve_image(
        self,
        image: RemoteImage,
        sleep=None,
    ) -> bytes:
        client = NanoBananaClient(lambda: None)
        if sleep is None:
            return client.resolve_image(image)
        return client.resolve_image(image, sleep=sleep)

    def _client_for_route(self, route: ProviderModelRoute, key: str):
        def key_provider() -> str:
            return key

        if route.protocol == PROTOCOL_OPENAI_CHAT_IMAGE:
            return OpenAIChatImageClient(
                key_provider,
                api_root=route.base_url,
                provider_model=route.provider_model,
                transport=self.transport,
            )
        if route.protocol == PROTOCOL_OPENAI_IMAGES:
            return OpenAIImagesClient(
                key_provider,
                api_root=route.base_url,
                provider_model=route.provider_model,
                transport=self.transport,
            )
        if route.protocol == PROTOCOL_LEGACY_JSON_IMAGES:
            return NanoBananaClient(
                key_provider,
                api_root=route.base_url,
                provider_model=route.provider_model,
                transport=self.transport,
            )
        raise ConfigurationError(f"不支持的 API 协议：{route.protocol}")

    @staticmethod
    def _can_failover(error: ApiError) -> bool:
        if error.error_code in {"timeout", "http_client"}:
            return False
        if error.error_code == "network":
            return True
        status = error.status_code
        return bool(
            status in {401, 403, 429}
            or (status is not None and status >= 500)
        )

    def _record_call(
        self,
        request: ImageRequest,
        provider_id: int,
        provider_name: str,
        provider_model: str,
        *,
        status: str,
        request_id: str = "",
        remote_task_id: str = "",
        status_code: int | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        if request.central_attempt_id is None:
            return
        try:
            self.repository.record_provider_call(
                request.central_attempt_id,
                self.actor,
                provider_id=provider_id,
                provider_name=provider_name,
                provider_model=provider_model,
                status=status,
                request_id=request_id,
                remote_task_id=remote_task_id,
                status_code=status_code,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            return
