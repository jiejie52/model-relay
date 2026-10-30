from __future__ import annotations

import copy
import json
from typing import Any

import httpx

from .base import ProviderHTTPError, ProviderRequestError
from .http_wire import decode_entity, observed_response_facts, read_raw_response
from .v2_base import V2ExecutionContext, V2ProviderResult
from ..config import Settings
from ..core.idempotency import CANONICAL_REQUEST_VERSIONS
from ..structured_output import project_schema_for_provider, resolve_structured_output
from .gemini_physical import (
    GEMINI_PHYSICAL_LAYOUT_VERSION,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
    SUPPORTED_GEMINI_PHYSICAL_LAYOUT_VERSIONS,
)
from .gemini_wire import (
    project_current_user_content,
    project_history_contents,
    project_system_instruction,
    validate_cached_content_handle,
)


class GeminiNativeAdapter:
    """Gemini native generateContent over AIHubMix Gemini Native Proxy."""

    adapter_version = "gemini-native-aihubmix/8"
    _PROTECTED = {"contents", "systemInstruction", "cachedContent", "model"}
    _CACHED_INFERENCE_FORBIDDEN = {"systemInstruction", "tools", "toolConfig"}

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

        # AIHubMix follows the Gemini CachedContent contract here: once a
        # cachedContent reference is present, systemInstruction/tools/toolConfig
        # must come from the CachedContent resource, not generateContent. Run
        # this after all option/structured-output projection so no legacy
        # provider_payload field can re-introduce a forbidden top-level key.
        self._enforce_cached_inference_wire(payload, context)

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
        transport_history: dict[str, Any] = {
            "kind": "gemini_native",
            "user_content": user_content,
            "model_content": model_content,
        }
        physical = context.provider_physical_plan
        if isinstance(physical, dict) and physical.get("layout_version") in SUPPORTED_GEMINI_PHYSICAL_LAYOUT_VERSIONS:
            transport_history.update(
                {
                    "physical_layout_version": physical.get("layout_version"),
                    "physical_projector_version": physical.get("projector_version"),
                    "physical_plan_hash": physical.get("physical_plan_hash"),
                    "material_occurrences": copy.deepcopy(physical.get("current_material_occurrences") or []),
                }
            )

        return V2ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=response_id,
            usage=usage,
            cached_tokens=usage_meta.get("cachedContentTokenCount"),
            response_output=model_content,
            history_entry={"transport_history": transport_history},
            http_status=status,
            provider_request_id=self._request_id(response_headers),
            observed=observed_response_facts(
                response_headers,
                body_model=(str(data.get("modelVersion")) if data.get("modelVersion") else None),
                protocol="gemini_native",
                channel_id=str(context.snapshot.get("channel_id") or "aihubmix"),
            ),
        )

    @classmethod
    def _enforce_cached_inference_wire(
        cls,
        payload: dict[str, Any],
        context: V2ExecutionContext,
    ) -> None:
        physical = context.provider_physical_plan
        if not isinstance(physical, dict):
            return
        if physical.get("layout_version") not in {
            GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
            GEMINI_PHYSICAL_LAYOUT_VERSION,
            GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
        }:
            return
        if not payload.get("cachedContent"):
            return

        for key in cls._CACHED_INFERENCE_FORBIDDEN:
            payload.pop(key, None)

        leaked = sorted(key for key in cls._CACHED_INFERENCE_FORBIDDEN if key in payload)
        if leaked:
            raise ProviderRequestError(
                "GEMINI_CACHED_INFERENCE_WIRE_INVALID",
                f"Cached Gemini inference wire still contains forbidden fields: {', '.join(leaked)}",
            )


    @staticmethod
    def _build_context_payload(context: V2ExecutionContext) -> tuple[dict[str, Any], dict[str, Any]]:
        binding = context.cache_execution_binding
        if not isinstance(binding, dict):
            raw = context.snapshot.get("_relay_cache_execution")
            binding = raw if isinstance(raw, dict) else {}

        physical = context.provider_physical_plan
        if isinstance(physical, dict) and physical.get("layout_version") in SUPPORTED_GEMINI_PHYSICAL_LAYOUT_VERSIONS:
            current_user = copy.deepcopy(physical.get("history_entry_user_content") or {})
            if not isinstance(current_user, dict) or current_user.get("role") != "user":
                raise ProviderRequestError(
                    "GEMINI_PHYSICAL_PLAN_INVALID",
                    "Gemini physical plan has no canonical current user Content",
                )
            if binding.get("mechanism") == "implicit_prefix":
                prefix = binding.get("prefix") if isinstance(binding.get("prefix"), dict) else {}
                if physical.get("layout_version") != GEMINI_PHYSICAL_LAYOUT_VERSION_V9:
                    raise ProviderRequestError(
                        "GEMINI_IMPLICIT_PREFIX_LAYOUT_UNSUPPORTED",
                        "Gemini implicit-prefix binding requires physical layout/9",
                    )
                expected_plan_hash = str(prefix.get("physical_plan_hash") or "")
                actual_plan_hash = str(physical.get("physical_plan_hash") or "")
                expected_prefix_fingerprint = str(prefix.get("prefix_fingerprint") or "")
                actual_prefix_fingerprint = str(physical.get("implicit_prefix_fingerprint") or "")
                expected_payload_hash = str(prefix.get("implicit_prefix_payload_hash") or "")
                actual_payload_hash = str(physical.get("implicit_prefix_payload_hash") or "")
                physical_dependencies = physical.get("dependencies") if isinstance(physical.get("dependencies"), dict) else {}
                try:
                    expected_prefix_version = int(prefix.get("prefix_version"))
                    actual_prefix_version = int(physical_dependencies.get("safe_history_prefix_version") or 0)
                except Exception as exc:
                    raise ProviderRequestError(
                        "CACHE_CONTEXT_MISMATCH",
                        "Gemini implicit-prefix binding has no valid prefix version",
                    ) from exc
                if (
                    not expected_plan_hash
                    or expected_plan_hash != actual_plan_hash
                    or not expected_prefix_fingerprint
                    or expected_prefix_fingerprint != actual_prefix_fingerprint
                    or expected_prefix_version != actual_prefix_version
                    or str(prefix.get("layout_version") or "") != str(physical.get("layout_version") or "")
                    or str(prefix.get("projector_version") or "") != str(physical.get("projector_version") or "")
                    or (expected_payload_hash and expected_payload_hash != actual_payload_hash)
                ):
                    raise ProviderRequestError(
                        "CACHE_CONTEXT_MISMATCH",
                        "Gemini implicit-prefix binding does not match the frozen physical plan",
                    )
                payload = copy.deepcopy(physical.get("implicit_prefix_payload") or {})
                if not isinstance(payload.get("contents"), list) or "cachedContent" in payload:
                    raise ProviderRequestError(
                        "GEMINI_PHYSICAL_PLAN_INVALID",
                        "Gemini implicit-prefix projection must contain complete contents without a CachedContent handle",
                    )
                return payload, current_user

            if binding.get("mechanism") == "stateful_resource":
                handle = validate_cached_content_handle(binding.get("provider_handle"))
                metadata = binding.get("metadata") if isinstance(binding.get("metadata"), dict) else {}
                binding_prefix = binding.get("prefix") if isinstance(binding.get("prefix"), dict) else {}
                expected_plan_hash = str(metadata.get("physical_plan_hash") or "")
                actual_plan_hash = str(physical.get("physical_plan_hash") or "")
                if expected_plan_hash and expected_plan_hash != actual_plan_hash:
                    raise ProviderRequestError(
                        "CACHE_CONTEXT_MISMATCH",
                        "Gemini CacheExecutionBinding does not match the frozen physical plan",
                    )
                if physical.get("layout_version") == GEMINI_PHYSICAL_LAYOUT_VERSION_V9:
                    cached_history_version = metadata.get("cached_history_version")
                    if cached_history_version is None:
                        cached_history_version = binding_prefix.get("prefix_version")
                    try:
                        cached_history_version = int(cached_history_version)
                    except Exception as exc:
                        raise ProviderRequestError(
                            "CACHE_CONTEXT_MISMATCH",
                            "Gemini layout/9 binding has no valid cached history prefix version",
                        ) from exc
                    if binding_prefix.get("prefix_version") is not None and int(binding_prefix["prefix_version"]) != cached_history_version:
                        raise ProviderRequestError(
                            "CACHE_CONTEXT_MISMATCH",
                            "Gemini layout/9 binding prefix version differs from its history boundary",
                        )
                    compatible_fingerprints = physical.get("compatible_prefix_fingerprints")
                    expected_content_fingerprint = (
                        str(compatible_fingerprints.get(str(cached_history_version)) or "")
                        if isinstance(compatible_fingerprints, dict)
                        else ""
                    )
                    actual_content_fingerprint = str(binding_prefix.get("content_fingerprint") or "")
                    if not expected_content_fingerprint or expected_content_fingerprint != actual_content_fingerprint:
                        raise ProviderRequestError(
                            "CACHE_CONTEXT_MISMATCH",
                            "Gemini layout/9 CachedContent fingerprint does not match the selected compatible prefix",
                        )
                    suffix_map = physical.get("uncached_suffix_by_prefix_version")
                    payload_source = suffix_map.get(str(cached_history_version)) if isinstance(suffix_map, dict) else None
                    suffix_hashes = physical.get("uncached_suffix_wire_hashes")
                    actual_suffix_hash = (
                        str(suffix_hashes.get(str(cached_history_version)) or "")
                        if isinstance(suffix_hashes, dict)
                        else ""
                    )
                else:
                    payload_source = physical.get("uncached_suffix")
                    actual_suffix_hash = str(physical.get("uncached_suffix_wire_hash") or "")
                expected_suffix_hash = str(metadata.get("uncached_suffix_wire_hash") or "")
                if expected_suffix_hash and expected_suffix_hash != actual_suffix_hash:
                    raise ProviderRequestError(
                        "CACHE_CONTEXT_MISMATCH",
                        "Gemini cached execution suffix differs from the selected frozen history boundary",
                    )
                payload = copy.deepcopy(payload_source or {})
                if not isinstance(payload.get("contents"), list):
                    raise ProviderRequestError(
                        "GEMINI_PHYSICAL_PLAN_INVALID",
                        "Gemini physical plan has no uncached suffix contents for the selected prefix version",
                    )
                payload["cachedContent"] = handle
                return payload, current_user

            # Final cache resolution may be None because caching was off, the
            # exact prefix was below Provider minimum, or auto degraded before
            # model dispatch. In every case send the complete uncached context.
            payload = copy.deepcopy(physical.get("full_uncached_payload") or {})
            if not isinstance(payload.get("contents"), list):
                raise ProviderRequestError(
                    "GEMINI_PHYSICAL_PLAN_INVALID",
                    "Gemini physical plan has no complete uncached contents",
                )
            return payload, current_user

        # Legacy Sessions retain the 3.1 committed-history projection.
        current_user = project_current_user_content(
            context.snapshot,
            material_ids=context.material_ids,
            material_bindings=context.material_bindings,
        )
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
            contents = project_history_contents(context.history, start_entry=cached_history_version)
            contents.append(current_user)
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
