import json
from typing import Any

import httpx

from ..config import Settings
from .base import ProviderHTTPError, ProviderRequestError, ProviderResult
from .http_wire import read_raw_response, decode_entity
from ..structured_output import (
    StructuredOutputError,
    apply_openai_responses_structured_output,
)


class OpenAICompatibleResponsesProvider:
    """OpenAI-compatible /responses adapter.

    The Relay infrastructure is model-agnostic. Current special handling is kept
    inside this provider adapter, including Grok store=false, encrypted reasoning
    replay, prompt_cache_key and reasoning.effort mapping.
    """

    _PROTECTED_OVERRIDE_KEYS = {
        "model",
        "input",
        "store",
        "include",
        "instructions",
        "prompt_cache_key",
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def execute(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any] | None,
        material_prefix: Any,
        history: list[dict[str, Any]],
    ) -> ProviderResult:
        provider = str(request_snapshot.get("provider", "")).lower()
        model = str(request_snapshot["model"])
        think_level = str(request_snapshot.get("think_level") or "auto").lower()

        input_items: list[Any] = []
        input_items.extend(self._material_items(material_prefix))

        for turn in history:
            user_item = turn.get("user_item")
            if user_item:
                input_items.append(user_item)
            previous_output = turn.get("response_output") or []
            if isinstance(previous_output, list):
                input_items.extend(previous_output)

        current_user_item = self.make_user_item(str(request_snapshot["current_query"]))
        prefix_has_query = bool(request_snapshot.get("material_prefix_includes_current_query"))
        if not (request_snapshot.get("mode") == "new_session" and prefix_has_query):
            input_items.append(current_user_item)

        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            # This Relay is intentionally responsible for external history.
            "store": False,
        }

        instructions = request_snapshot.get("instructions")
        if instructions:
            payload["instructions"] = instructions

        if self._is_grok(provider, model):
            payload["include"] = ["reasoning.encrypted_content"]
            if session and session.get("prompt_cache_key"):
                payload["prompt_cache_key"] = session["prompt_cache_key"]
            if think_level in {"low", "medium", "high", "xhigh"}:
                payload["reasoning"] = {"effort": think_level}

        provider_payload = request_snapshot.get("provider_payload") or {}
        if isinstance(provider_payload, dict):
            for key, value in provider_payload.items():
                if key not in self._PROTECTED_OVERRIDE_KEYS:
                    payload[key] = value

        # Generic structured-output passthrough. The adapter never inspects the
        # schema's business property names; it only maps the caller's JSON Schema
        # to the OpenAI-compatible Responses transport. This deliberately runs
        # after provider_payload merge so an explicit caller schema overrides any
        # legacy/fallback json_object setting.
        try:
            apply_openai_responses_structured_output(
                payload,
                request_snapshot,
                fallback_name=str(request_snapshot.get("stage") or "structured_output"),
            )
        except StructuredOutputError as exc:
            raise ProviderRequestError(exc.code, exc.message) from exc

        base_url = (
            ((request_snapshot.get("upstream") or {}).get("base_url"))
            or self.settings.aihubmix_root
        ).rstrip("/")
        url = f"{base_url}/responses"

        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=None,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        if self.settings.aihubmix_api_key is None:
            raise ProviderRequestError(
                "CONNECTION_NOT_CONFIGURED",
                "AIHUBMIX_API_KEY is not configured for this connection",
            )
        headers = {
            "Authorization": f"Bearer {self.settings.aihubmix_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            if hasattr(client, "stream"):
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    raw = await read_raw_response(response, log_context=request_snapshot.get("_relay_log_context"))
                    response_status = response.status_code
                    response_headers = dict(getattr(response, "headers", {}) or {})
            else:  # test doubles / older compatible clients
                response = await client.post(url, headers=headers, json=payload)
                raw = await response.aread()
                response_status = response.status_code
                response_headers = dict(getattr(response, "headers", {}) or {})

        if response_status < 200 or response_status >= 300:
            raise ProviderHTTPError(
                response_status,
                raw,
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=(response_headers.get("x-request-id") or response_headers.get("request-id")),
            )

        try:
            decoded = decode_entity(raw, response_headers.get("content-encoding"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            raise ProviderHTTPError(
                response_status,
                raw,
                "Upstream response was not valid JSON",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=(response_headers.get("x-request-id") or response_headers.get("request-id")),
            ) from exc

        text = self.extract_visible_text(data)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cached_tokens = self.extract_cached_tokens(usage)
        output = data.get("output") if isinstance(data.get("output"), list) else []

        return ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=data.get("id"),
            usage=usage,
            cached_tokens=cached_tokens,
            response_output=output,
            http_status=response_status,
            provider_request_id=(response_headers.get("x-request-id") or response_headers.get("request-id")),
        )

    @staticmethod
    def make_user_item(text: str) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }

    @classmethod
    def _material_items(cls, material_prefix: Any) -> list[Any]:
        if material_prefix is None:
            return []
        if isinstance(material_prefix, list):
            return material_prefix
        if isinstance(material_prefix, str):
            return [cls.make_user_item(material_prefix)]
        if isinstance(material_prefix, dict):
            maybe_input = material_prefix.get("input")
            if isinstance(maybe_input, list):
                return maybe_input
            # Already an input/message item.
            if "role" in material_prefix or "type" in material_prefix:
                return [material_prefix]
            # Generic wrapper for structured material supplied by Dify.
            return [
                cls.make_user_item(
                    "[Relay material prefix JSON]\n"
                    + json.dumps(material_prefix, ensure_ascii=False)
                )
            ]
        return [cls.make_user_item(str(material_prefix))]

    @staticmethod
    def _is_grok(provider: str, model: str) -> bool:
        return provider in {"grok", "xai"} or model.lower().startswith("grok-")

    @staticmethod
    def extract_visible_text(data: dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]

        pieces: list[str] = []
        output = data.get("output")
        if not isinstance(output, list):
            return ""

        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "output_text" and isinstance(item.get("text"), str):
                pieces.append(item["text"])

            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_text", "text"} and isinstance(
                    part.get("text"), str
                ):
                    pieces.append(part["text"])
        return "\n".join(piece for piece in pieces if piece)

    @staticmethod
    def extract_cached_tokens(usage: dict[str, Any]) -> int | None:
        candidates = [
            (usage.get("input_tokens_details") or {}).get("cached_tokens"),
            (usage.get("input_details") or {}).get("cached_tokens"),
            usage.get("cached_tokens"),
        ]
        for value in candidates:
            if isinstance(value, int):
                return value
        return None
