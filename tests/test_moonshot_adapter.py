import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import SecretStr

from app.providers.base import ProviderHTTPError, ProviderRequestError
from app.providers.moonshot import MoonshotChatCompletionsProvider
from app.structured_output import apply_openai_chat_structured_output


SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}


def settings():
    return SimpleNamespace(
        moonshot_api_key=SecretStr("secret"),
        moonshot_root="https://api.moonshot.ai/v1",
        upstream_connect_timeout_seconds=1.0,
        upstream_write_timeout_seconds=1.0,
        upstream_pool_timeout_seconds=1.0,
    )


class _Headers:
    def __init__(self, values):
        self._values = {str(k).lower(): str(v) for k, v in values}
        self.raw = [
            (str(k).encode("latin-1"), str(v).encode("latin-1"))
            for k, v in values
        ]

    def get(self, name, default=None):
        return self._values.get(str(name).lower(), default)


class _Response:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = _Headers(headers or [("content-type", "application/json")])

    async def aread(self):
        return self._body


class _Client:
    response = None
    captured_json = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).captured_json = json
        return type(self).response


class MoonshotAdapterTests(unittest.TestCase):
    def test_k3_reasoning_effort_is_explicit_and_strict(self):
        payload = {}
        MoonshotChatCompletionsProvider._apply_reasoning(
            payload,
            "kimi-k3",
            {"reasoning": {"effort": "max"}},
            {"think_level": "auto"},
        )
        self.assertEqual(payload["reasoning_effort"], "max")

        with self.assertRaises(ProviderRequestError):
            MoonshotChatCompletionsProvider._apply_reasoning(
                {},
                "kimi-k3",
                {},
                {"think_level": "medium"},
            )

    def test_k26_uses_thinking_object(self):
        payload = {}
        MoonshotChatCompletionsProvider._apply_reasoning(
            payload,
            "kimi-k2.6",
            {"thinking": {"type": "disabled"}},
            {"think_level": "auto"},
        )
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_chat_structured_output_uses_response_format(self):
        payload = {}
        spec = apply_openai_chat_structured_output(
            payload,
            {
                "provider": "moonshot",
                "model": "kimi-k3",
                "structured_output": {"mode": "json_schema", "schema": SCHEMA},
            },
            provider="moonshot",
            model="kimi-k3",
        )
        self.assertEqual(spec.schema, SCHEMA)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], SCHEMA)

    def test_remote_media_url_fails_closed(self):
        with self.assertRaises(ProviderRequestError) as ctx:
            MoonshotChatCompletionsProvider._normalize_message(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.invalid/a.png"},
                        }
                    ],
                }
            )
        self.assertEqual(ctx.exception.code, "MOONSHOT_REMOTE_MEDIA_URL_UNSUPPORTED")

    def test_provider_error_preserves_original_bytes_and_headers(self):
        raw = b'{"error":{"message":"rate limited","vendor_field":123}}\n'
        _Client.response = _Response(
            429,
            raw,
            [("content-type", "application/json"), ("x-request-id", "req-vendor")],
        )
        provider = MoonshotChatCompletionsProvider(settings())
        request = {
            "provider": "moonshot",
            "model": "kimi-k3",
            "input": [{"role": "user", "content": "hello"}],
            "generation": {"reasoning": {"effort": "low"}},
        }
        with patch("app.providers.moonshot.httpx.AsyncClient", _Client):
            with self.assertRaises(ProviderHTTPError) as ctx:
                asyncio.run(
                    provider.execute(
                        request,
                        session=None,
                        material_prefix=None,
                        history=[],
                    )
                )
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.body, raw)
        self.assertIn(("x-request-id", "req-vendor"), ctx.exception.headers)
        self.assertEqual(ctx.exception.request_id, "req-vendor")

    def test_complete_assistant_message_is_preserved_for_history(self):
        response_body = {
            "id": "cmpl-1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_content": "hidden-but-provider-required",
                        "tool_calls": [{"id": "call-1", "type": "function"}],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        _Client.response = _Response(200, json.dumps(response_body).encode("utf-8"))
        provider = MoonshotChatCompletionsProvider(settings())
        request = {
            "provider": "moonshot",
            "model": "kimi-k3",
            "input": [{"role": "user", "content": "hello"}],
            "generation": {"reasoning": {"effort": "high"}},
        }
        with patch("app.providers.moonshot.httpx.AsyncClient", _Client):
            result = asyncio.run(
                provider.execute(request, session={"prompt_cache_key": "session-1"}, material_prefix=None, history=[])
            )
        assistant = result.history_record["messages"][-1]
        self.assertEqual(assistant["reasoning_content"], "hidden-but-provider-required")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-1")
        self.assertEqual(_Client.captured_json["prompt_cache_key"], "session-1")


if __name__ == "__main__":
    unittest.main()
