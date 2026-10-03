from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

import httpx

from ...config import Settings
from ...core.idempotency import stable_hash
from ...providers.base import ProviderHTTPError, ProviderRequestError
from ...providers.gemini_genai_sdk import GeminiAIHubMixGenAIClient
from ...providers.gemini_physical import (
    GEMINI_PHYSICAL_LAYOUT_VERSION,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V5,
)
from ...providers.gemini_wire import (
    PROJECTION_VERSION,
    prefix_fingerprint,
    prefix_payload,
    prefix_reuse_key,
    validate_cached_content_handle,
)

class GeminiAIHubMixCacheResourceAdapter:
    """Gemini explicit CachedContent lifecycle over the AIHubMix native proxy.

    The adapter owns Provider wire only. Policy, idempotency and Request
    dispatch rights remain in Relay core. Layout/5 preserves the historical
    google-genai File -> caches.create() path for already-frozen Sessions.
    Layout/6+ also uses google-genai, but CachedContent receives inline bytes only;
    Gemini Files API is reserved for inference-only material. Layout/8 additionally
    requires non-empty CachedContent.contents before the stateful operation may be
    claimed. Layout/4 and older Sessions preserve their frozen native-REST
    cache-create semantics.
    """

    adapter_version = "gemini-cache-aihubmix/8"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sdk: GeminiAIHubMixGenAIClient | Any | None = None,
    ) -> None:
        if not settings.aihubmix_gemini_base_url:
            raise RuntimeError("AIHUBMIX_GEMINI_BASE_URL is required for Gemini cache")
        if settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required for Gemini cache")
        self.settings = settings
        self.base_url = settings.aihubmix_gemini_base_url.rstrip("/")
        self._transport = transport
        self.sdk = sdk or GeminiAIHubMixGenAIClient(settings)

    def build_spec(
        self,
        *,
        snapshot: dict[str, Any],
        history: list[dict[str, Any]],
        context_plan: dict[str, Any],
        material_bindings: list[dict[str, Any]],
        session: dict[str, Any],
        ttl_seconds: int | None,
        provider_physical_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(provider_physical_plan, dict):
            physical = provider_physical_plan
            payload = dict(physical.get("cached_prefix") or {})
            layout_version = str(physical.get("layout_version") or "")
            if layout_version in {
                GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
                GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
                GEMINI_PHYSICAL_LAYOUT_VERSION,
            } and self._payload_has_file_data(payload):
                raise ProviderRequestError(
                    "GEMINI_CACHE_FILE_REF_FORBIDDEN",
                    "Layout/6+ CachedContent must contain inline static data only; Gemini File references are inference-only",
                )
            physical_cacheable = bool(physical.get("cacheable"))
            if layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION:
                # Layout/8 must never claim a stateful cache operation for a
                # systemInstruction-only prefix.  The resource adapter is the
                # final protocol boundary before StatefulResourceManager may
                # look up or create a Provider resource, so repeat the
                # non-empty-contents invariant here even though the projector
                # already enforces it.
                physical_cacheable = physical_cacheable and bool(payload.get("contents"))

            return {
                "schema_version": str((physical.get("cache_spec") or {}).get("schema_version") or "relay-gemini-cache-spec/2"),
                "projection_version": str(physical.get("projector_version") or ""),
                "layout_version": layout_version,
                "physical_plan_hash": str(physical.get("physical_plan_hash") or ""),
                "cached_prefix_wire_hash": str(physical.get("cached_prefix_wire_hash") or ""),
                "uncached_suffix_wire_hash": str(physical.get("uncached_suffix_wire_hash") or ""),
                "occurrence_mapping_hash": str(physical.get("occurrence_mapping_hash") or ""),
                "cache_spec": dict(physical.get("cache_spec") or {}),
                "model": str(physical.get("model") or snapshot.get("model") or ""),
                "prefix_version": int(physical.get("prefix_version") or 0),
                "reuse_key": str(physical.get("reuse_key") or ""),
                "content_fingerprint": str(physical.get("content_fingerprint") or ""),
                "compatible_prefix_fingerprints": dict(physical.get("compatible_prefix_fingerprints") or {}),
                "provider_payload": payload,
                "cacheable": physical_cacheable,
                "ttl_seconds": int(ttl_seconds) if isinstance(ttl_seconds, int) and ttl_seconds > 0 else 3600,
                "context_plan_hash": context_plan.get("context_plan_hash"),
                "material_binding_hash": stable_hash(material_bindings),
                "measurement_order": str(physical.get("measurement_order") or "before_lookup"),
                "cache_create_transport": (
                    "google_genai_sdk"
                    if layout_version in {
                        GEMINI_PHYSICAL_LAYOUT_VERSION_V5,
                        GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
                        GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
                        GEMINI_PHYSICAL_LAYOUT_VERSION,
                    }
                    else "native_rest"
                ),
                "sdk_file_refs": (
                    self._sdk_file_refs(payload=payload, material_bindings=material_bindings)
                    if layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V5
                    else []
                ),
            }

        # Legacy 3.1 Sessions keep their frozen projection. The current user
        # input/materials are deliberately excluded.  The
        # reusable prefix is: system instruction + committed history turns.
        # The current request and history after a reused prefix remain suffix.
        prefix_version = len(history) if session.get("context_policy") == "conversation" else 0
        payload = prefix_payload(
            snapshot=snapshot,
            history=history,
            history_entries=prefix_version,
        )
        cacheable = bool(payload.get("contents") or payload.get("systemInstruction"))
        compatible: dict[str, str] = {}
        if cacheable:
            # Exact fingerprints for every committed-turn boundary allow a
            # previous generation to remain reusable after history grows.
            for version in range(prefix_version + 1):
                candidate = prefix_payload(
                    snapshot=snapshot,
                    history=history,
                    history_entries=version,
                )
                if not (candidate.get("contents") or candidate.get("systemInstruction")):
                    continue
                compatible[str(version)] = prefix_fingerprint(
                    snapshot=snapshot,
                    history=history,
                    history_entries=version,
                )

        return {
            "schema_version": "relay-gemini-cache-spec/1",
            "projection_version": PROJECTION_VERSION,
            "model": str(snapshot.get("model") or ""),
            "prefix_version": prefix_version,
            "reuse_key": prefix_reuse_key(snapshot),
            "content_fingerprint": (
                prefix_fingerprint(
                    snapshot=snapshot,
                    history=history,
                    history_entries=prefix_version,
                )
                if cacheable
                else ""
            ),
            "compatible_prefix_fingerprints": compatible,
            "provider_payload": payload,
            "cacheable": cacheable,
            "ttl_seconds": int(ttl_seconds) if isinstance(ttl_seconds, int) and ttl_seconds > 0 else 3600,
            "context_plan_hash": context_plan.get("context_plan_hash"),
            "material_binding_hash": stable_hash(material_bindings),
        }

    async def measure(self, *, spec: dict[str, Any]) -> int:
        model = self._model(spec)
        provider_payload = dict(spec.get("provider_payload") or {})
        generate_request: dict[str, Any] = {"model": f"models/{model}"}
        generate_request.update(provider_payload)
        data, _ = await self._request_json(
            "POST",
            f"{self.base_url}/v1beta/models/{model}:countTokens",
            json_body={"generateContentRequest": generate_request},
            phase="gemini_cache_count_tokens",
        )
        count = data.get("totalTokens")
        if isinstance(count, bool):
            count = None
        try:
            value = int(count)
        except Exception as exc:
            raise RuntimeError("Gemini countTokens response has no totalTokens") from exc
        if value < 0:
            raise RuntimeError("Gemini countTokens returned a negative totalTokens")
        return value

    async def create(self, *, spec: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
        model = self._model(spec)
        if (
            str(spec.get("layout_version") or "") == GEMINI_PHYSICAL_LAYOUT_VERSION
            and not (spec.get("provider_payload") or {}).get("contents")
        ):
            # Defense in depth: build_spec() should make this spec non-cacheable
            # so StatefulResourceManager never claims a create operation.  If a
            # caller bypasses that gate, still forbid any Provider side effect.
            raise ProviderRequestError(
                "GEMINI_CACHE_EMPTY_CONTENTS_FORBIDDEN",
                "Layout/8 CachedContent.create requires non-empty contents",
            )
        if str(spec.get("cache_create_transport") or "") == "google_genai_sdk":
            cache = await self.sdk.create_cache(
                model=model,
                cached_prefix=dict(spec.get("provider_payload") or {}),
                file_refs=[
                    dict(x) for x in (spec.get("sdk_file_refs") or []) if isinstance(x, dict)
                ],
                display_name=f"relay-{str(spec.get('content_fingerprint') or '')[:24]}",
                ttl_seconds=int(spec.get("ttl_seconds") or 3600),
            )
            data = self.sdk.dump_model(cache)
            raw_handle = data.get("name") or getattr(cache, "name", None)
            handle = self.validate_handle(raw_handle) if raw_handle not in (None, "") else None
            usage = data.get("usageMetadata") or data.get("usage_metadata") or {}
            return {
                "handle": handle,
                "expire_time": data.get("expireTime") or data.get("expire_time"),
                "create_time": data.get("createTime") or data.get("create_time"),
                "usage_metadata": usage if isinstance(usage, dict) else {},
                "provider_request_id": self.sdk.request_id(cache),
                "operation_epoch": int(operation.get("lease_epoch") or 0),
                "provider_transport": "google_genai_sdk",
                "sdk_adapter_version": str(getattr(self.sdk, "adapter_version", "")),
            }

        body: dict[str, Any] = {
            "model": f"models/{model}",
            "ttl": f"{int(spec.get('ttl_seconds') or 3600)}s",
            "displayName": f"relay-{str(spec.get('content_fingerprint') or '')[:24]}",
        }
        body.update(dict(spec.get("provider_payload") or {}))
        data, headers = await self._request_json(
            "POST",
            f"{self.base_url}/v1beta/cachedContents",
            json_body=body,
            phase="gemini_cache_create",
        )
        raw_handle = data.get("name")
        handle = self.validate_handle(raw_handle) if raw_handle not in (None, "") else None
        return {
            "handle": handle,
            "expire_time": data.get("expireTime"),
            "create_time": data.get("createTime"),
            "usage_metadata": data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {},
            "provider_request_id": self._request_id(headers),
            "operation_epoch": int(operation.get("lease_epoch") or 0),
            "provider_transport": "native_rest",
        }

    async def get(self, *, handle: str, operation: dict[str, Any] | None = None) -> dict[str, Any]:
        safe = self.validate_handle(handle)
        data, headers = await self._request_json(
            "GET",
            f"{self.base_url}/v1beta/{safe}",
            phase="gemini_cache_get",
        )
        return {
            "handle": self.validate_handle(data.get("name") or safe),
            "expire_time": data.get("expireTime"),
            "create_time": data.get("createTime"),
            "usage_metadata": data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {},
            "provider_request_id": self._request_id(headers),
        }

    async def renew(
        self,
        *,
        handle: str,
        expire_at: str,
        operation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe = self.validate_handle(handle)
        data, headers = await self._request_json(
            "PATCH",
            f"{self.base_url}/v1beta/{safe}",
            params={"updateMask": "expire_time"},
            json_body={"expireTime": expire_at},
            phase="gemini_cache_renew",
        )
        return {
            "handle": self.validate_handle(data.get("name") or safe),
            "expire_time": data.get("expireTime"),
            "provider_request_id": self._request_id(headers),
        }

    async def delete(self, *, handle: str, operation: dict[str, Any] | None = None) -> dict[str, Any]:
        safe = self.validate_handle(handle)
        _, headers = await self._request_json(
            "DELETE",
            f"{self.base_url}/v1beta/{safe}",
            phase="gemini_cache_delete",
            allow_empty=True,
        )
        return {"handle": safe, "deleted": True, "provider_request_id": self._request_id(headers)}

    @staticmethod
    def _sdk_file_refs(*, payload: dict[str, Any], material_bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bindings_by_uri: dict[str, dict[str, Any]] = {}
        for binding in material_bindings:
            uri = str(binding.get("file_uri") or binding.get("external_uri") or "")
            if uri:
                bindings_by_uri[uri] = binding

        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for content in payload.get("contents") or []:
            if not isinstance(content, dict):
                continue
            for part in content.get("parts") or []:
                if not isinstance(part, dict) or "fileData" not in part:
                    continue
                file_data = part.get("fileData") or {}
                uri = str(file_data.get("fileUri") or file_data.get("file_uri") or "")
                binding = bindings_by_uri.get(uri) or {}
                name = str(binding.get("provider_file_id") or binding.get("external_file_id") or "")
                mime_type = str(
                    file_data.get("mimeType")
                    or file_data.get("mime_type")
                    or (binding.get("metadata") or {}).get("mime_type")
                    or binding.get("content_type")
                    or ""
                )
                if not uri or not name:
                    raise RuntimeError(
                        "Current Gemini SDK cache layout requires a frozen Files API name and URI"
                    )
                identity = (name, uri)
                if identity in seen:
                    continue
                seen.add(identity)
                refs.append({"name": name, "uri": uri, "mime_type": mime_type})
        return refs

    @staticmethod
    def _payload_has_file_data(payload: dict[str, Any]) -> bool:
        for content in payload.get("contents") or []:
            if not isinstance(content, dict):
                continue
            for part in content.get("parts") or []:
                if isinstance(part, dict) and "fileData" in part:
                    return True
        return False

    @staticmethod
    def validate_handle(value: Any) -> str:
        try:
            return validate_cached_content_handle(value)
        except Exception as exc:
            raise RuntimeError("Gemini CachedContent handle is not an allowed relative resource name") from exc

    @staticmethod
    def is_expired(observation: dict[str, Any], *, safety_seconds: int = 0) -> bool:
        raw = observation.get("expire_time")
        if not raw:
            return False
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return True
        return parsed.timestamp() <= datetime.now(timezone.utc).timestamp() + max(0, safety_seconds)

    def _model(self, spec: dict[str, Any]) -> str:
        model = str(spec.get("model") or "").strip()
        if not model or "/" in model:
            raise RuntimeError("Invalid Gemini model in cache spec")
        return model

    def _headers(self) -> dict[str, str]:
        assert self.settings.aihubmix_api_key is not None
        return {
            "x-goog-api-key": self.settings.aihubmix_api_key.get_secret_value(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        phase: str,
        allow_empty: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.cache_prepare_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout, verify=True, transport=self._transport) as client:
            response = await client.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
            )
        raw = response.content
        headers = dict(response.headers)
        if not 200 <= response.status_code < 300:
            raise ProviderHTTPError(
                response.status_code,
                raw,
                content_type=headers.get("content-type"),
                content_encoding=headers.get("content-encoding"),
                request_id=self._request_id(headers),
                response_headers=headers,
                phase=phase,
            )
        if not raw:
            if allow_empty:
                return {}, headers
            raise RuntimeError(f"{phase} returned an empty success response")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"{phase} success response is not JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"{phase} success response is not an object")
        return data, headers

    @staticmethod
    def _request_id(headers: dict[str, str]) -> str | None:
        lowered = {str(k).lower(): v for k, v in headers.items()}
        for key in ("x-request-id", "request-id", "x-goog-request-id", "x-cloud-trace-context"):
            if lowered.get(key):
                return str(lowered[key])
        return None
