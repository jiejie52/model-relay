from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import Settings
from ..structured_output import (
    StructuredOutputError,
    apply_openai_responses_structured_output,
)
from .base import (
    ProviderHTTPError,
    ProviderRequestError,
    ProviderResult,
    ProviderTransportError,
)


class OpenAICompatibleResponsesProvider:
    """AIHubMix/OpenAI-compatible `/responses` adapter.

    Provider-specific behavior remains in this adapter. Relay Core only persists
    the request snapshot, provider-native history record and output references.
    """

    provider_id = "openai-compatible"
    protocol = "openai-responses"
    history_codec = "openai-responses/1"

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

    @staticmethod
    def capability_profile_version(model: str) -> str:
        return "openai-compatible-responses/1"

    async def execute(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any] | None,
        material_prefix: Any,
        history: list[dict[str, Any]],
    ) -> ProviderResult:
        if self.settings.aihubmix_api_key is None:
            raise ProviderRequestError(
                "AIHUBMIX_CREDENTIAL_MISSING",
                "AIHUBMIX_API_KEY is not configured for this provider profile",
            )

        provider = str(request_snapshot.get("provider", "")).lower()
        model = str(request_snapshot["model"])
        think_level = str(request_snapshot.get("think_level") or "auto").lower()

        input_items: list[Any] = []
        input_items.extend(self._material_items(material_prefix))

        for turn in history:
            if not isinstance(turn, dict):
                continue
            request_items = turn.get("request_items")
            if isinstance(request_items, list):
                input_items.extend(request_items)
            else:
                user_item = turn.get("user_item")
                if user_item:
                    input_items.append(user_item)
            previous_output = turn.get("response_output") or turn.get("assistant_output") or []
            if isinstance(previous_output, list):
                input_items.extend(previous_output)

        current_items = self._current_request_items(request_snapshot)
        prefix_has_query = bool(request_snapshot.get("material_prefix_includes_current_query"))
        if request_snapshot.get("mode") == "new_session" and prefix_has_query:
            current_items = []
        input_items.extend(current_items)

        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "store": False,
        }

        instructions = request_snapshot.get("instructions")
        if instructions:
            payload["instructions"] = instructions

        generation = request_snapshot.get("generation")
        if isinstance(generation, dict):
            for key, value in generation.items():
                if key not in self._PROTECTED_OVERRIDE_KEYS and key not in {"reasoning"}:
                    payload[key] = value

        if self._is_grok(provider, model):
            payload["include"] = ["reasoning.encrypted_content"]
            if session and session.get("prompt_cache_key"):
                payload["prompt_cache_key"] = session["prompt_cache_key"]
            reasoning = generation.get("reasoning") if isinstance(generation, dict) else None
            if isinstance(reasoning, dict) and reasoning.get("effort"):
                payload["reasoning"] = {"effort": str(reasoning["effort"])}
            elif think_level in {"low", "medium", "high", "xhigh"}:
                payload["reasoning"] = {"effort": think_level}

        provider_payload = request_snapshot.get("provider_payload") or {}
        if isinstance(provider_payload, dict):
            for key, value in provider_payload.items():
                if key not in self._PROTECTED_OVERRIDE_KEYS:
                    payload[key] = value

        try:
            apply_openai_responses_structured_output(
                payload,
                request_snapshot,
                fallback_name=str(
                    request_snapshot.get("stage")
                    or request_snapshot.get("label")
                    or "structured_output"
                ),
            )
        except StructuredOutputError as exc:
            raise ProviderRequestError(exc.code, exc.message) from exc

        # Legacy v1 may still supply upstream.base_url. New v2 requests use the
        # server-side profile and do not expose arbitrary credential-bearing URLs.
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
        headers = {
            "Authorization": f"Bearer {self.settings.aihubmix_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
                response = await client.post(url, headers=headers, json=payload)
                raw = await response.aread()
        except httpx.HTTPError as exc:
            raise ProviderTransportError(
                str(exc),
                provider=provider or "openai-compatible",
                service="aihubmix",
                exception_type=type(exc).__name__,
                cause_chain=self._cause_chain(exc),
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise ProviderHTTPError(
                response.status_code,
                raw,
                headers=self._headers_raw(response),
                content_type=response.headers.get("content-type"),
                provider=provider or "openai-compatible",
                service="aihubmix",
                request_id=self._request_id(response),
            )

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ProviderHTTPError(
                response.status_code,
                raw,
                headers=self._headers_raw(response),
                content_type=response.headers.get("content-type"),
                provider=provider or "openai-compatible",
                service="aihubmix",
                request_id=self._request_id(response),
                message="Upstream success response was not valid JSON",
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
            history_record={
                "codec": self.history_codec,
                "request_items": current_items,
                "response_id": data.get("id"),
                "response_output": output,
            },
        )

    @staticmethod
    def make_user_item(text: str) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }

    @classmethod
    def _current_request_items(cls, request_snapshot: dict[str, Any]) -> list[Any]:
        value = request_snapshot.get("input")
        if isinstance(value, list):
            out: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    out.append(cls.make_user_item(item))
                elif isinstance(item, dict):
                    out.append(cls._normalize_v2_message(item))
            return out
        query = request_snapshot.get("current_query")
        if query is None:
            return []
        return [cls.make_user_item(str(query))]

    @classmethod
    def _normalize_v2_message(cls, item: dict[str, Any]) -> dict[str, Any]:
        if "role" not in item:
            return item
        content = item.get("content")
        if isinstance(content, str):
            return {"role": item.get("role"), "content": [{"type": "input_text", "text": content}]}
        if isinstance(content, list):
            normalized = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    normalized.append({"type": "input_text", "text": str(part.get("text") or "")})
                else:
                    normalized.append(part)
            clone = dict(item)
            clone["content"] = normalized
            return clone
        return item

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
            if "role" in material_prefix or "type" in material_prefix:
                return [material_prefix]
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

    @staticmethod
    def _headers_raw(response: httpx.Response) -> list[tuple[str, str]]:
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in response.headers.raw
        ]

    @staticmethod
    def _request_id(response: httpx.Response) -> str | None:
        for name in ("x-request-id", "request-id", "x-correlation-id"):
            value = response.headers.get(name)
            if value:
                return value
        return None

    @staticmethod
    def _cause_chain(exc: BaseException) -> list[str]:
        out: list[str] = []
        current: BaseException | None = exc
        while current is not None and len(out) < 8:
            out.append(f"{type(current).__name__}: {current}")
            current = current.__cause__ or current.__context__
        return out
