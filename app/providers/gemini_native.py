from __future__ import annotations

import json
from typing import Any

import httpx

from .base import ProviderHTTPError, ProviderRequestError
from .http_wire import decode_entity, read_raw_response
from .v2_base import V2ExecutionContext, V2ProviderResult
from ..config import Settings
from ..materials.gemini_transport import project_gemini_input_content_type
from ..structured_output import project_schema_for_provider, resolve_structured_output


class GeminiNativeAdapter:
    """Gemini native generateContent over AIHubMix Gemini Native Proxy."""

    adapter_version = "gemini-native-aihubmix/2"
    _PROTECTED = {"contents", "systemInstruction", "model"}

    def __init__(self, settings: Settings) -> None:
        if not settings.aihubmix_gemini_base_url:
            raise RuntimeError("AIHUBMIX_GEMINI_BASE_URL is required for Gemini native")
        if settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required for Gemini native")
        self.settings = settings
        self.base_url = settings.aihubmix_gemini_base_url.rstrip("/")

    async def execute(self, context: V2ExecutionContext) -> V2ProviderResult:
        contents: list[dict[str, Any]] = []
        if context.session.get("context_policy") == "conversation":
            for turn in context.history:
                transport = turn.get("transport_history") if isinstance(turn, dict) else None
                if not isinstance(transport, dict) or transport.get("kind") != "gemini_native":
                    continue
                user_content = transport.get("user_content")
                model_content = transport.get("model_content")
                if isinstance(user_content, dict):
                    contents.append(user_content)
                if isinstance(model_content, dict):
                    contents.append(model_content)

        current_parts: list[dict[str, Any]] = []
        by_id = {str(x.get("material_id")): x for x in context.material_bindings}
        for material_id in context.material_ids:
            binding = by_id.get(material_id)
            if not binding:
                raise ProviderRequestError(
                    "MATERIAL_BINDING_MISSING",
                    f"Frozen Gemini binding missing for {material_id}",
                )
            file_uri = binding.get("external_uri")
            if not file_uri:
                raise ProviderRequestError(
                    "MATERIAL_BINDING_INVALID",
                    f"Gemini binding has no file URI for {material_id}",
                )
            current_parts.append(
                {
                    "fileData": {
                        "mimeType": project_gemini_input_content_type(binding.get("content_type")),
                        "fileUri": str(file_uri),
                    }
                }
            )

        value = context.snapshot.get("input")
        if isinstance(value, str):
            query_text = value
        else:
            query_text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        current_parts.append({"text": query_text})
        user_content = {"role": "user", "parts": current_parts}
        contents.append(user_content)

        payload: dict[str, Any] = {"contents": contents}
        instructions = context.snapshot.get("instructions")
        if instructions:
            payload["systemInstruction"] = {"parts": [{"text": str(instructions)}]}

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
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
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
            cached_tokens=None,
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
        )

    @classmethod
    def _apply_model_options(cls, payload: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """Project canonical Relay options to Gemini Native generationConfig.

        v2.2 never merges caller dictionaries into the native request. For an
        already-persisted v2.1 Request, known sampling keys are translated to
        generationConfig so a pre-upgrade temperature Request can resume safely;
        other legacy keys retain their former compatibility behavior.
        """
        if str(snapshot.get("schema_version") or "") == "relay-request/2.2":
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
