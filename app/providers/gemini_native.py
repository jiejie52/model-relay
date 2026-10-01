from __future__ import annotations

import json
from typing import Any

import httpx

from .base import ProviderHTTPError, ProviderRequestError
from .http_wire import decode_entity, observed_response_facts, read_raw_response
from .v2_base import V2ExecutionContext, V2ProviderResult
from ..config import Settings
from ..core.idempotency import CANONICAL_REQUEST_VERSIONS
from ..structured_output import project_schema_for_provider, resolve_structured_output
from .gemini_wire import (
    project_current_user_content,
    project_history_contents,
    project_system_instruction,
    validate_cached_content_handle,
)


class GeminiNativeAdapter:
    """Gemini native generateContent over AIHubMix Gemini Native Proxy."""

    adapter_version = "gemini-native-aihubmix/3"
    _PROTECTED = {"contents", "systemInstruction", "cachedContent", "model"}

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.aihubmix_gemini_base_url:
            raise RuntimeError("AIHUBMIX_GEMINI_BASE_URL is required for Gemini native")
        if settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required for Gemini native")
        self.settings = settings
        self.base_url = settings.aihubmix_gemini_base_url.rstrip("/")
        self._transport = transport

    async def execute(self, context: V2ExecutionContext) -> V2ProviderResult:
        payload, user_content = self._build_context_payload(context)

        self._apply_model_options(payload, context.snapshot)

        spec = resolve_structured_output(
            context.snapshot,
            fallback_name=str(context.snapshot.get("metadata", {}).get("stage") or "structured_output"),
        )
        if spec is not None:
            generation = payload.get("generationConfig")
            if not isinstance(generation, dict):
                generation = {}
                payload["generationConfig"] = generation
            if spec.mode == "json_object":
                generation["responseMimeType"] = "application/json"
            elif spec.mode == "json_schema" and spec.schema is not None:
                projection = project_schema_for_provider(
                    spec.schema,
                    provider="gemini",
                    model=context.snapshot.get("model"),
                )
                generation["responseMimeType"] = "application/json"
                generation["responseSchema"] = projection.schema

        model = str(context.snapshot["model"])
        url = f"{self.base_url}/v1beta/models/{model}:generateContent"
        headers = {
            "x-goog-api-key": self.settings.aihubmix_api_key.get_secret_value(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=None,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout, verify=True, transport=self._transport) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                raw = await read_raw_response(response, log_context={"request_id": context.request_id, "session_id": context.session_id, "provider": "gemini", "connection_id": context.snapshot.get("connection_id"), "model": context.snapshot.get("model")})
                status = response.status_code
                response_headers = dict(response.headers)
        if not 200 <= status < 300:
            raise ProviderHTTPError(
                status,
                raw,
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
                response_headers=response_headers,
                phase="gemini_generate_content",
            )
        try:
            decoded = decode_entity(raw, response_headers.get("content-encoding"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            raise ProviderHTTPError(
                status,
                raw,
                "Gemini success response was not valid JSON",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
                response_headers=response_headers,
                phase="gemini_generate_content_parse",
            ) from exc

        candidates = data.get("candidates") if isinstance(data, dict) else None
        first = candidates[0] if isinstance(candidates, list) and candidates else None
        model_content = first.get("content") if isinstance(first, dict) else None
        if not isinstance(model_content, dict):
            raise ProviderHTTPError(
                status,
                raw,
                "Gemini response has no candidate content",
                content_type=response_headers.get("content-type"),
                request_id=self._request_id(response_headers),
                response_headers=response_headers,
                phase="gemini_generate_content_parse",
            )
        text_parts: list[str] = []
        for part in model_content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
        text = "\n".join(text_parts)
        usage_meta = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        usage = {
            "prompt_tokens": usage_meta.get("promptTokenCount"),
            "completion_tokens": usage_meta.get("candidatesTokenCount"),
            "total_tokens": usage_meta.get("totalTokenCount"),
            "raw": usage_meta,
        }
        response_id = str(data.get("responseId") or data.get("id") or "") or None
        return V2ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=response_id,
            usage=usage,
            cached_tokens=usage_meta.get("cachedContentTokenCount"),
            response_output=model_content,
            history_entry={
                "transport_history": {
                    "kind": "gemini_native",
                    "user_content": user_content,
                    "model_content": model_content,
                }
            },
            http_status=status,
            provider_request_id=self._request_id(response_headers),
            observed=observed_response_facts(
                response_headers,
                body_model=(str(data.get("modelVersion")) if data.get("modelVersion") else None),
                protocol="gemini_native",
                channel_id=str(context.snapshot.get("channel_id") or "aihubmix"),
            ),
        )


    @staticmethod
    def _build_context_payload(context: V2ExecutionContext) -> tuple[dict[str, Any], dict[str, Any]]:
        current_user = project_current_user_content(
            context.snapshot,
            material_ids=context.material_ids,
            material_bindings=context.material_bindings,
        )
        binding = context.cache_execution_binding
        if not isinstance(binding, dict):
            raw = context.snapshot.get("_relay_cache_execution")
            binding = raw if isinstance(raw, dict) else {}

        if binding.get("mechanism") == "stateful_resource":
            handle = validate_cached_content_handle(binding.get("provider_handle"))
            metadata = binding.get("metadata") if isinstance(binding.get("metadata"), dict) else {}
            cached_history_version = metadata.get("cached_history_version")
            if isinstance(cached_history_version, bool) or not isinstance(cached_history_version, int):
                raise ProviderRequestError(
                    "CACHE_BINDING_INVALID",
                    "Gemini stateful cache binding has no committed-history prefix boundary",
                )
            if cached_history_version < 0 or cached_history_version > len(context.history):
                raise ProviderRequestError(
                    "CACHE_BINDING_INVALID",
                    "Gemini cached history boundary is outside the frozen history",
                )
            contents = project_history_contents(
                context.history, start_entry=cached_history_version
            )
            contents.append(current_user)
            # CachedContent already carries the system instruction and committed
            # prefix.  Never repeat either in the generateContent request.
            return {"contents": contents, "cachedContent": handle}, current_user

        contents = project_history_contents(context.history)
        contents.append(current_user)
        payload: dict[str, Any] = {"contents": contents}
        instruction = project_system_instruction(context.snapshot)
        if instruction is not None:
            payload["systemInstruction"] = instruction
        return payload, current_user

    @classmethod
    def _apply_model_options(cls, payload: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """Project canonical Relay options to Gemini Native generationConfig.

        v2.2 never merges caller dictionaries into the native request. For an
        already-persisted v2.1 Request, known sampling keys are translated to
        generationConfig so a pre-upgrade temperature Request can resume safely;
        other legacy keys retain their former compatibility behavior.
        """
        if str(snapshot.get("schema_version") or "") in CANONICAL_REQUEST_VERSIONS:
            options = snapshot.get("effective_options") or {}
            if not isinstance(options, dict):
                return
            generation = payload.get("generationConfig")
            if not isinstance(generation, dict):
                generation = {}
                payload["generationConfig"] = generation
            if "temperature" in options:
                generation["temperature"] = options["temperature"]
            if "top_p" in options:
                generation["topP"] = options["top_p"]
            if "max_output_tokens" in options:
                generation["maxOutputTokens"] = options["max_output_tokens"]
            think_level = str(snapshot.get("think_level") or "auto").lower()
            if think_level in {"low", "medium", "high"}:
                generation["thinkingConfig"] = {"thinkingLevel": think_level.upper()}
            if not generation:
                payload.pop("generationConfig", None)
            return

        provider_payload = snapshot.get("provider_payload") or {}
        if not isinstance(provider_payload, dict):
            return
        generation = payload.get("generationConfig")
        if not isinstance(generation, dict):
            generation = {}
        legacy_generation = provider_payload.get("generationConfig")
        if isinstance(legacy_generation, dict):
            generation.update(legacy_generation)
        if "temperature" in provider_payload:
            generation["temperature"] = provider_payload["temperature"]
        if "top_p" in provider_payload:
            generation["topP"] = provider_payload["top_p"]
        if "max_output_tokens" in provider_payload:
            generation["maxOutputTokens"] = provider_payload["max_output_tokens"]
        if "max_tokens" in provider_payload and "maxOutputTokens" not in generation:
            generation["maxOutputTokens"] = provider_payload["max_tokens"]
        if generation:
            payload["generationConfig"] = generation

        translated = {
            "generationConfig", "temperature", "top_p", "max_output_tokens",
            "max_tokens", "material_mode",
        }
        for key, val in provider_payload.items():
            if key in translated or key in cls._PROTECTED:
                continue
            payload[key] = val

    @staticmethod
    def _request_id(headers: Any) -> str | None:
        for key in ("x-request-id", "request-id", "x-goog-request-id", "x-cloud-trace-context"):
            value = headers.get(key)
            if value:
                return str(value)
        return None
