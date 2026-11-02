from __future__ import annotations

import base64
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from tests.helpers import png_bytes
from yixin.api import OpenAIChatImageClient, OpenAIImagesClient
from yixin.collaboration.types import (
    AuthUser,
    ProviderModelRoute,
)
from yixin.constants import (
    PROTOCOL_LEGACY_JSON_IMAGES,
    PROTOCOL_OPENAI_CHAT_IMAGE,
    PROTOCOL_OPENAI_IMAGES,
)
from yixin.models import ApiError, ImageRequest, StoredImage
from yixin.provider_router import ProviderRouter


class FakeKeyStore:
    def __init__(self, keys: dict[int, str]) -> None:
        self.keys = keys
        self.loaded: list[int | None] = []

    def load(self, *, provider_id=None, strict=False):
        self.loaded.append(provider_id)
        value = self.keys.get(int(provider_id or 0))
        if strict and not value:
            raise RuntimeError("尚未配置 API Key")
        return value


class FakeProviderRepository:
    def __init__(self, routes: list[ProviderModelRoute]) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    def list_provider_routes(self, _model, *, provider_code=None):
        if provider_code:
            return [
                route
                for route in self.routes
                if route.provider_code == provider_code
            ]
        return list(self.routes)

    def record_provider_call(self, _attempt_id, _actor, **kwargs):
        self.calls.append(kwargs)


class ProviderRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.image_path = Path(self.temp.name) / "reference.png"
        self.image_path.write_bytes(png_bytes((20, 80, 140)))
        self.reference = StoredImage(
            path=self.image_path,
            content_hash="a" * 64,
            mime="image/png",
            width=10,
            height=10,
            original_name="reference.png",
        )
        self.actor = AuthUser(9, "ai", "AI", "ai")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _route(
        provider_id: int,
        provider_code: str,
        provider_name: str,
        protocol: str,
        *,
        max_references: int = 14,
    ) -> ProviderModelRoute:
        return ProviderModelRoute(
            provider_id=provider_id,
            provider_code=provider_code,
            provider_name=provider_name,
            base_url=f"https://{provider_code}.example/v1",
            default_protocol=protocol,
            logical_model="gemini-3-pro-image",
            provider_model="gemini-3-pro-image",
            protocol=protocol,
            max_reference_images=max_references,
            images_per_request=4,
            priority=provider_id,
        )

    def _request(self, *, n: int = 1, refs: bool = True) -> ImageRequest:
        return ImageRequest(
            model="gemini-3-pro-image",
            prompt="将衣服穿到模特身上",
            n=n,
            aspect_ratio="3:4",
            resolution="2K",
            reference_images=(self.reference,) if refs else (),
            mode="reference" if refs else "text",
        )

    def test_88api_chat_adapter_reads_markdown_data_image(self) -> None:
        captured: dict = {}
        image_b64 = base64.b64encode(
            png_bytes((1, 2, 3))
        ).decode("ascii")

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["auth"] = request.headers.get("authorization")
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    f"![image](data:image/png;base64,{image_b64})"
                                )
                            }
                        }
                    ],
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 22,
                    },
                },
                headers={"X-Request-Id": "req-88"},
            )

        client = OpenAIChatImageClient(
            lambda: "sk-test-123456",
            api_root="https://api.88api.ai/v1",
            provider_model="gemini-3-pro-image",
            transport=httpx.MockTransport(handler),
        )
        result = client.generate(self._request())
        self.assertEqual(captured["path"], "/v1/chat/completions")
        self.assertEqual(
            captured["auth"],
            "Bearer sk-test-123456",
        )
        self.assertEqual(
            captured["payload"]["google"]["image_config"],
            {"aspect_ratio": "3:4", "image_size": "2K"},
        )
        self.assertEqual(
            captured["payload"]["messages"][0]["content"][1]["type"],
            "image_url",
        )
        self.assertEqual(result.request_id, "req-88")
        self.assertEqual(result.usage_input_tokens, 11)
        self.assertEqual(result.usage_output_tokens, 22)
        self.assertTrue(result.images[0].b64_json)

    def test_openai_images_edit_uses_multipart_references(self) -> None:
        captured: dict = {}
        image_b64 = base64.b64encode(
            png_bytes((4, 5, 6))
        ).decode("ascii")

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["content_type"] = request.headers["content-type"]
            captured["body"] = request.content
            return httpx.Response(
                200,
                json={"data": [{"b64_json": image_b64}]},
            )

        client = OpenAIImagesClient(
            lambda: "sk-test-123456",
            api_root="https://api.a6api.vip/v1",
            provider_model="gemini-3-pro-image",
            transport=httpx.MockTransport(handler),
        )
        result = client.generate(self._request())
        self.assertEqual(captured["path"], "/v1/images/edits")
        self.assertIn(
            "multipart/form-data",
            captured["content_type"],
        )
        self.assertIn(b'name="image[]"', captured["body"])
        self.assertEqual(len(result.images), 1)

    def test_openai_images_generation_without_references_uses_json(self) -> None:
        captured: dict = {}
        image_b64 = base64.b64encode(png_bytes()).decode("ascii")

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["content_type"] = request.headers["content-type"]
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"data": [{"b64_json": image_b64}]},
            )

        client = OpenAIImagesClient(
            lambda: "sk-test-123456",
            api_root="https://gateway.amux.ai/v1",
            provider_model="gemini-3-pro-image",
            transport=httpx.MockTransport(handler),
        )
        client.generate(self._request(refs=False))
        self.assertEqual(
            captured["path"],
            "/v1/images/generations",
        )
        self.assertIn("application/json", captured["content_type"])
        self.assertEqual(
            captured["payload"]["aspect_ratio"],
            "3:4",
        )

    def test_88api_chat_adapter_expands_multiple_requested_images(self) -> None:
        count = {"value": 0}
        image_b64 = base64.b64encode(png_bytes()).decode("ascii")

        def handler(_request: httpx.Request) -> httpx.Response:
            count["value"] += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    f"![image](data:image/png;base64,{image_b64})"
                                )
                            }
                        }
                    ]
                },
            )

        client = OpenAIChatImageClient(
            lambda: "sk-test-123456",
            api_root="https://api.88api.ai/v1",
            provider_model="gemini-3-pro-image",
            transport=httpx.MockTransport(handler),
        )
        result = client.generate(self._request(n=2))
        self.assertEqual(count["value"], 2)
        self.assertEqual(len(result.images), 2)

    def test_router_fails_over_on_500(self) -> None:
        routes = [
            self._route(
                3,
                "a6api",
                "A6API",
                PROTOCOL_OPENAI_IMAGES,
            ),
            self._route(
                4,
                "amux",
                "Amux",
                PROTOCOL_OPENAI_IMAGES,
            ),
        ]
        repository = FakeProviderRepository(routes)
        keys = FakeKeyStore({3: "sk-a6-123456", 4: "sk-amux-123456"})
        image_b64 = base64.b64encode(png_bytes()).decode("ascii")
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            if request.url.host == "a6api.example":
                return httpx.Response(500, json={"error": {"message": "繁忙"}})
            return httpx.Response(
                200,
                json={"data": [{"b64_json": image_b64}]},
            )

        router = ProviderRouter(
            repository,
            keys,
            self.actor,
            transport=httpx.MockTransport(handler),
        )
        result = router.generate(
            replace(self._request(refs=False), central_attempt_id=7)
        )
        self.assertEqual(seen, ["a6api.example", "amux.example"])
        self.assertEqual(result.provider_code, "amux")
        self.assertEqual(
            [call["status"] for call in repository.calls],
            ["failed", "succeeded"],
        )

    def test_manual_provider_is_preferred_then_fails_over(self) -> None:
        routes = [
            self._route(
                3,
                "a6api",
                "A6API",
                PROTOCOL_OPENAI_IMAGES,
            ),
            self._route(
                4,
                "amux",
                "Amux",
                PROTOCOL_OPENAI_IMAGES,
            ),
        ]
        seen: list[str] = []
        image_b64 = base64.b64encode(png_bytes()).decode("ascii")

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            if request.url.host == "amux.example":
                return httpx.Response(500, json={"error": {"message": "繁忙"}})
            return httpx.Response(
                200,
                json={"data": [{"b64_json": image_b64}]},
            )

        router = ProviderRouter(
            FakeProviderRepository(routes),
            FakeKeyStore({3: "sk-a6-123456", 4: "sk-amux-123456"}),
            self.actor,
            transport=httpx.MockTransport(handler),
        )
        request = replace(
            self._request(refs=False),
            provider_code="amux",
        )
        result = router.generate(request)
        self.assertEqual(seen, ["amux.example", "a6api.example"])
        self.assertEqual(result.provider_code, "a6api")

    def test_router_does_not_fail_over_after_timeout(self) -> None:
        routes = [
            self._route(
                1,
                "liuguang",
                "流光 API",
                PROTOCOL_LEGACY_JSON_IMAGES,
            ),
            self._route(
                4,
                "amux",
                "Amux",
                PROTOCOL_OPENAI_IMAGES,
            ),
        ]
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            raise httpx.ReadTimeout("timeout", request=request)

        router = ProviderRouter(
            FakeProviderRepository(routes),
            FakeKeyStore(
                {
                    1: "sk-liuguang-123456",
                    4: "sk-amux-123456",
                }
            ),
            self.actor,
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ApiError) as caught:
            router.generate(self._request(refs=False))
        self.assertEqual(caught.exception.error_code, "timeout")
        self.assertEqual(calls["count"], 1)

    def test_router_skips_route_without_enough_reference_capacity(self) -> None:
        repository = FakeProviderRepository(
            [
                self._route(
                    2,
                    "88api",
                    "88API",
                    PROTOCOL_OPENAI_CHAT_IMAGE,
                    max_references=0,
                )
            ]
        )
        router = ProviderRouter(
            repository,
            FakeKeyStore({2: "sk-88api-123456"}),
            self.actor,
        )
        with self.assertRaisesRegex(Exception, "参考图"):
            router.generate(self._request())

    def test_failover_policy_keeps_ambiguous_timeout_on_same_platform(self) -> None:
        for status, expected in (
            (401, True),
            (403, True),
            (429, True),
            (500, True),
            (400, False),
            (None, False),
        ):
            with self.subTest(status=status):
                error = ApiError(
                    "test",
                    status_code=status,
                    error_code="timeout" if status is None else "http_error",
                )
                self.assertEqual(
                    ProviderRouter._can_failover(error),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
