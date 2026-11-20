from __future__ import annotations
from app.runtime.execution_guard import ensure_execution_allowed, ExecutionLeaseLost

import base64
import io
import json
from typing import Any

from app.runtime.transport_guard import provider_transport
from app.config import Settings
from app.protocol_adapters.base import ProviderHTTPError, ProviderRequestError


class GeminiGenAIClient:
    """Small testable wrapper around the official ``google-genai`` SDK.

    Layout/5 uses the SDK for the historical Files -> CachedContent path.
    Layout/6 uses the same SDK for CachedContent creation from inline bytes and
    reserves Files API for inference-only material. Relay still owns durable
    resource identities, fencing and retry policy. The in-memory File registry
    remains only an optimization for frozen layout/5 Sessions and inference
    File bindings.
    """

    adapter_version = "google-genai-aihubmix/2"

    def __init__(
        self,
        settings: Settings,
        *,
        connection=None,
        client: Any | None = None,
        types_module: Any | None = None,
    ) -> None:
        if connection is None:
            from app.compatibility.connection_config import legacy_runtime
            connection = legacy_runtime(settings, "gemini_native")
        self.connection = connection
        self.settings = settings
        self.base_url = connection.sdk_base
        self._client = client
        self._types_module = types_module
        self._files_by_name = {}
        self._files_by_uri = {}

    def _types(self) -> Any:
        if self._types_module is not None:
            return self._types_module
        try:
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError(
                "google-genai is required for the Gemini SDK execution"
            ) from exc
        self._types_module = types
        return types

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError(
                "google-genai is required for the Gemini SDK execution"
            ) from exc
        types = self._types()

        # The current SDK calls this field ``base_url``. Keep retries explicit:
        # cache/file side effects are fenced and reconciled by Relay, not by an
        # opaque SDK retry loop.
        import httpx
        class AsyncOnlyTransport(httpx.BaseTransport):
            def handle_request(self, request):
                raise RuntimeError("Synchronous SDK network calls are not authorized")
        http_options = types.HttpOptions(
            base_url=self.base_url,
            api_version=self.connection.api_version,
            headers=self.connection.headers(),
            retry_options=types.HttpRetryOptions(attempts=1),
            client_args={"trust_env": False, "transport": AsyncOnlyTransport()},
            async_client_args={"trust_env": False, "transport": provider_transport(self.settings, "gemini", url=self.base_url, always=True, connection=self.connection)},
        )
        self._client = genai.Client(
            api_key=self.connection.secret,
            http_options=http_options,
        )
        return self._client

    @property
    def client(self) -> Any:
        return self._ensure_client()

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aio.aclose()
        finally:
            try:
                self._client.close()
            finally:
                self._client = None

    def remember_file(self, file_obj: Any) -> Any:
        name = str(getattr(file_obj, "name", "") or "")
        uri = str(getattr(file_obj, "uri", "") or "")
        if name:
            self._files_by_name[name] = file_obj
        if uri:
            self._files_by_uri[uri] = file_obj
        return file_obj

    def cached_file(self, *, name: str | None = None, uri: str | None = None) -> Any | None:
        if name and name in self._files_by_name:
            return self._files_by_name[name]
        if uri and uri in self._files_by_uri:
            return self._files_by_uri[uri]
        return None

    async def upload_file(
        self,
        *,
        data: bytes,
        filename: str,
        mime_type: str,
    ) -> Any:
        types = self._types()
        stream = io.BytesIO(data)
        stream.name = filename  # type: ignore[attr-defined]
        try:
            await ensure_execution_allowed(states=("not_sent",))
            file_obj = await self.client.aio.files.upload(
                file=stream,
                config=types.UploadFileConfig(
                    display_name=filename,
                    mime_type=mime_type,
                ),
            )
        except ExecutionLeaseLost:
            raise
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_files_upload") from exc
        await ensure_execution_allowed(states=('not_sent',))
        return self.remember_file(file_obj)

    async def get_file(self, *, name: str, refresh: bool = False) -> Any:
        cached = self.cached_file(name=name)
        if cached is not None and not refresh:
            return cached
        try:
            await ensure_execution_allowed(states=("not_sent",))
            file_obj = await self.client.aio.files.get(name=name)
        except ExecutionLeaseLost:
            raise
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_files_get") from exc
        return self.remember_file(file_obj)

    async def delete_file(self, *, name: str) -> None:
        try:
            await ensure_execution_allowed(states=("not_sent",))
            await self.client.aio.files.delete(name=name)
        except ExecutionLeaseLost:
            raise
        except Exception as exc:
            # The SDK raises a typed ClientError for an absent file. Preserve the
            # normal error unless the provider explicitly reports 404.
            error = self._provider_error(exc, phase="gemini_files_delete")
            if isinstance(error, ProviderHTTPError) and error.status_code == 404:
                return
            raise error from exc
        self._files_by_name.pop(name, None)
        for uri, file_obj in list(self._files_by_uri.items()):
            if str(getattr(file_obj, "name", "") or "") == name:
                self._files_by_uri.pop(uri, None)

    async def create_cache(
        self,
        *,
        model: str,
        cached_prefix: dict[str, Any],
        file_refs: list[dict[str, Any]],
        display_name: str,
        ttl_seconds: int,
    ) -> Any:
        types = self._types()

        contents = await self._sdk_contents(
            cached_prefix=cached_prefix,
            file_refs=file_refs,
            types_module=types,
        )
        system_instruction = self._sdk_system_instruction(
            cached_prefix.get("systemInstruction"),
            types_module=types,
        )
        try:
            await ensure_execution_allowed(states=("not_sent",))
            cache = await self.client.aio.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    display_name=display_name,
                    contents=contents,
                    system_instruction=system_instruction,
                    **({
                        "tools": cached_prefix.get("tools"),
                        "tool_config": cached_prefix.get("toolConfig"),
                    } if cached_prefix.get("tools") or cached_prefix.get("toolConfig") else {}),
                    ttl=f"{int(ttl_seconds)}s",
                ),
            )
        except ExecutionLeaseLost:
            raise
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_cache_create") from exc
        return cache

    async def get_cache(self, *, name: str) -> Any:
        try:
            await ensure_execution_allowed(states=("not_sent",))
            return await self.client.aio.caches.get(name=name)
        except ExecutionLeaseLost:
            raise
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_cache_get") from exc

    async def renew_cache(self, *, name: str, expire_at: str) -> Any:
        types = self._types()
        try:
            await ensure_execution_allowed(states=("not_sent",))
            return await self.client.aio.caches.update(
                name=name,
                config=types.UpdateCachedContentConfig(expire_time=expire_at),
            )
        except ExecutionLeaseLost:
            raise
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_cache_renew") from exc

    async def delete_cache(self, *, name: str) -> None:
        try:
            await ensure_execution_allowed(states=("not_sent",))
            await self.client.aio.caches.delete(name=name)
        except ExecutionLeaseLost:
            raise
        except Exception as exc:
            error = self._provider_error(exc, phase="gemini_cache_delete")
            if isinstance(error, ProviderHTTPError) and error.status_code == 404:
                return
            raise error from exc

    async def _sdk_contents(
        self,
        *,
        cached_prefix: dict[str, Any],
        file_refs: list[dict[str, Any]],
        types_module: Any,
    ) -> list[Any]:
        refs_by_uri = {
            str(ref.get("uri") or ""): ref
            for ref in file_refs
            if str(ref.get("uri") or "")
        }
        refs_by_name = {
            str(ref.get("name") or ""): ref
            for ref in file_refs
            if str(ref.get("name") or "")
        }
        contents: list[Any] = []
        for content in cached_prefix.get("contents") or []:
            if not isinstance(content, dict):
                raise ProviderRequestError(
                    "GEMINI_CACHE_SDK_CONTENT",
                    "Gemini cached prefix contains a non-object content entry",
                )
            role = str(content.get("role") or "user")
            sdk_parts: list[Any] = []
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    raise ProviderRequestError(
                        "GEMINI_CACHE_SDK_PART",
                        "Gemini cached prefix contains a non-object part",
                    )
                if "fileData" in part:
                    file_data = part.get("fileData") or {}
                    uri = str(file_data.get("fileUri") or file_data.get("file_uri") or "")
                    ref = refs_by_uri.get(uri)
                    if ref is None:
                        # Some providers normalize URI spelling on GET. A name
                        # match remains authoritative inside the frozen binding.
                        candidate_name = str(file_data.get("name") or "")
                        ref = refs_by_name.get(candidate_name)
                    if ref is None:
                        raise ProviderRequestError(
                            "GEMINI_CACHE_FILE_REF_MISSING",
                            f"No frozen Gemini File reference matches cached URI {uri!r}",
                        )
                    if self.connection.spec.credential_ref:
                        sdk_parts.append(types_module.Part.from_uri(file_uri=uri,
                            mime_type=str(file_data.get("mimeType") or file_data.get("mime_type") or ref.get("mime_type") or "application/octet-stream")))
                        continue
                    name = str(ref.get("name") or "")
                    file_obj = self.cached_file(name=name, uri=uri)
                    if file_obj is None:
                        # Cross-process / restart-safe path: rehydrate the
                        # official File object, then pass that object to
                        # caches.create just like a direct files.upload result.
                        file_obj = await self.get_file(name=name)
                    sdk_parts.append(file_obj)
                    continue
                if "inlineData" in part:
                    inline = part.get("inlineData") or {}
                    raw = inline.get("data")
                    if not isinstance(raw, str):
                        raise ProviderRequestError(
                            "GEMINI_CACHE_INLINE_DATA",
                            "Gemini inlineData is missing base64 data",
                        )
                    try:
                        data = base64.b64decode(raw, validate=True)
                    except ExecutionLeaseLost:
                        raise
                    except Exception as exc:
                        raise ProviderRequestError(
                            "GEMINI_CACHE_INLINE_DATA",
                            "Gemini inlineData contains invalid base64",
                        ) from exc
                    sdk_parts.append(
                        types_module.Part.from_bytes(
                            data=data,
                            mime_type=str(inline.get("mimeType") or inline.get("mime_type") or "application/octet-stream"),
                        )
                    )
                    continue
                if "text" in part:
                    sdk_parts.append(types_module.Part.from_text(text=str(part.get("text") or "")))
                    continue
                raise ProviderRequestError(
                    "GEMINI_CACHE_SDK_PART_UNSUPPORTED",
                    f"Unsupported Gemini cached part keys: {sorted(part.keys())}",
                )

            if role == "user" and not self.connection.spec.credential_ref:
                # Keep raw File objects in the PartUnion list. google-genai's
                # content transformer converts File -> Part.from_uri while
                # preserving the single UserContent boundary.
                contents.append(sdk_parts)
            else:
                normalized_parts = []
                for item in sdk_parts:
                    if self._looks_like_file(item):
                        normalized_parts.append(
                            types_module.Part.from_uri(
                                file_uri=str(getattr(item, "uri", "") or ""),
                                mime_type=str(getattr(item, "mime_type", "") or ""),
                            )
                        )
                    else:
                        normalized_parts.append(item)
                contents.append(types_module.Content(role=role, parts=normalized_parts))
        return contents

    @staticmethod
    def _sdk_system_instruction(value: Any, *, types_module: Any) -> Any | None:
        if not isinstance(value, dict):
            return None
        parts = value.get("parts") or []
        texts: list[str] = []
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                texts.append(str(part.get("text") or ""))
        if not texts:
            return None
        # google-genai documents CreateCachedContentConfig.system_instruction
        # with the string form. Relay's canonical Request instruction projects
        # to one text part, so prefer that exact representation. Preserve the
        # existing Content form only if a future projection supplies multiple
        # parts.
        if len(texts) == 1:
            return texts[0]
        sdk_parts = [types_module.Part.from_text(text=text) for text in texts]
        return types_module.Content(role="user", parts=sdk_parts)

    @staticmethod
    def _looks_like_file(value: Any) -> bool:
        return bool(getattr(value, "uri", None) and getattr(value, "mime_type", None))

    @staticmethod
    def dump_model(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if hasattr(value, "model_dump"):
            data = value.model_dump(mode="json", by_alias=True, exclude_none=True)
            return data if isinstance(data, dict) else {"value": data}
        if isinstance(value, dict):
            return dict(value)
        return {
            key: item
            for key in (
                "name",
                "uri",
                "mime_type",
                "display_name",
                "state",
                "create_time",
                "expire_time",
                "usage_metadata",
            )
            if (item := getattr(value, key, None)) is not None
        }

    @classmethod
    def model_json_bytes(cls, value: Any) -> bytes:
        return json.dumps(cls.dump_model(value), ensure_ascii=False, default=str).encode("utf-8")

    @staticmethod
    def response_headers(value: Any) -> dict[str, str]:
        response = getattr(value, "sdk_http_response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            return {}
        try:
            return {str(k): str(v) for k, v in dict(headers).items()}
        except Exception:
            return {}

    @classmethod
    def request_id(cls, value: Any) -> str | None:
        lowered = {k.lower(): v for k, v in cls.response_headers(value).items()}
        for key in ("x-request-id", "request-id", "x-goog-request-id", "x-cloud-trace-context"):
            if lowered.get(key):
                return str(lowered[key])
        return None

    @classmethod
    def mount_continuation_handle(
        cls,
        config: Any,
        continuation_binding: dict[str, Any] | Any | None,
        *,
        types_module: Any | None = None,
    ) -> Any:
        """Mount continuation handle (e.g. cached_content or session handle) into SDK config/payload.

        When a valid continuation binding is present (mechanism != 'none' and not fallback_occurred),
        extracts upstream_handle and sets cached_content on GenerateContentConfig or dict.
        """
        if not continuation_binding:
            return config

        binding_dict = (
            continuation_binding.to_dict()
            if hasattr(continuation_binding, "to_dict")
            else dict(continuation_binding)
            if isinstance(continuation_binding, dict)
            else None
        )
        if not binding_dict:
            return config

        if binding_dict.get("fallback_occurred") or binding_dict.get("mechanism") == "none":
            return config

        upstream_handle = binding_dict.get("upstream_handle")
        if upstream_handle is None and isinstance(binding_dict.get("active_anchor"), dict):
            upstream_handle = binding_dict["active_anchor"].get("upstream_handle")

        if not upstream_handle:
            return config

        handle_str = str(upstream_handle)
        if config is None:
            if types_module and hasattr(types_module, "GenerateContentConfig"):
                return types_module.GenerateContentConfig(cached_content=handle_str)
            return {"cached_content": handle_str}

        if hasattr(config, "cached_content"):
            try:
                setattr(config, "cached_content", handle_str)
            except Exception:
                pass
        elif isinstance(config, dict):
            config["cached_content"] = handle_str
            config["cachedContent"] = handle_str
        return config

    @classmethod
    def extract_cache_facts(cls, response: Any) -> dict[str, Any]:
        """Extract cache facts (cached tokens, usage breakdown, cache hit) from response."""
        data = cls.dump_model(response) if not isinstance(response, dict) else response
        usage_meta = (
            data.get("usageMetadata")
            or data.get("usage_metadata")
            or getattr(response, "usage_metadata", None)
        )
        usage_dict = cls.dump_model(usage_meta) if not isinstance(usage_meta, dict) else (usage_meta or {})

        cached_tokens = (
            usage_dict.get("cachedContentTokenCount")
            or usage_dict.get("cached_content_token_count")
            or getattr(usage_meta, "cached_content_token_count", None)
        )
        prompt_tokens = (
            usage_dict.get("promptTokenCount")
            or usage_dict.get("prompt_token_count")
            or getattr(usage_meta, "prompt_token_count", None)
        )
        candidates_tokens = (
            usage_dict.get("candidatesTokenCount")
            or usage_dict.get("candidates_token_count")
            or getattr(usage_meta, "candidates_token_count", None)
        )
        total_tokens = (
            usage_dict.get("totalTokenCount")
            or usage_dict.get("total_token_count")
            or getattr(usage_meta, "total_token_count", None)
        )

        facts: dict[str, Any] = {
            "cached_tokens": cached_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": candidates_tokens,
            "total_tokens": total_tokens,
            "has_cache": bool(cached_tokens and cached_tokens > 0),
        }
        resp_id = (
            data.get("responseId")
            or data.get("response_id")
            or data.get("id")
            or getattr(response, "response_id", None)
            or getattr(response, "id", None)
        )
        if resp_id:
            facts["response_id"] = str(resp_id)
            facts["upstream_handle"] = str(resp_id)

        model_ver = (
            data.get("modelVersion")
            or data.get("model_version")
            or getattr(response, "model_version", None)
        )
        if model_ver:
            facts["model_version"] = str(model_ver)

        return facts

    @classmethod
    def extract_session_facts(
        cls,
        response: Any,
        *,
        continuation_binding: Any = None,
    ) -> dict[str, Any]:
        """Extract both cache and continuation session facts from response."""
        facts = cls.extract_cache_facts(response)
        binding_dict = (
            continuation_binding.to_dict()
            if hasattr(continuation_binding, "to_dict")
            else dict(continuation_binding)
            if isinstance(continuation_binding, dict)
            else None
        )
        if binding_dict:
            is_active = (
                binding_dict.get("mechanism") != "none"
                and not binding_dict.get("fallback_occurred", False)
            )
            facts["continuation_active"] = is_active
            if is_active:
                if binding_dict.get("active_anchor_id"):
                    facts["active_anchor_id"] = binding_dict["active_anchor_id"]
                if binding_dict.get("upstream_handle"):
                    facts["bound_upstream_handle"] = binding_dict["upstream_handle"]
        return facts

    async def generate_content(
        self,
        *,
        model: str,
        contents: Any,
        config: Any | None = None,
        continuation_binding: dict[str, Any] | None = None,
    ) -> Any:
        """Generate content via google-genai SDK with continuation handle support."""
        types = self._types()
        if continuation_binding:
            config = self.mount_continuation_handle(
                config,
                continuation_binding,
                types_module=types,
            )
        try:
            await ensure_execution_allowed(states=("not_sent",))
            kwargs: dict[str, Any] = {"model": model, "contents": contents}
            if config is not None:
                kwargs["config"] = config
            response = await self.client.aio.models.generate_content(**kwargs)
            return response
        except ExecutionLeaseLost:
            raise
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_generate_content") from exc

    @staticmethod
    def _provider_error(exc: Exception, *, phase: str) -> Exception:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(exc, "code", None)
        try:
            status_code = int(status) if status is not None else None
        except Exception:
            status_code = None

        response = getattr(exc, "response", None)
        headers: dict[str, str] = {}
        if response is not None:
            try:
                headers = {str(k): str(v) for k, v in dict(response.headers).items()}
            except Exception:
                headers = {}
        request_id = None
        lowered = {k.lower(): v for k, v in headers.items()}
        for key in ("x-request-id", "request-id", "x-goog-request-id", "x-cloud-trace-context"):
            if lowered.get(key):
                request_id = str(lowered[key])
                break

        # Prefer the exact HTTP entity returned by AIHubMix. google-genai
        # normally exposes an httpx-like response on APIError/ClientError.
        # 4.2.1 reconstructed the body from response_json, which could hide
        # provider details or change formatting.  The raw response bytes are
        # authoritative for diagnostics; parsed SDK data is only a fallback.
        body: bytes | None = None
        raw_body_source: str | None = None
        if response is not None:
            try:
                content = getattr(response, "content", None)
                if isinstance(content, bytes):
                    body = content
                    raw_body_source = "response.content"
                elif isinstance(content, (bytearray, memoryview)):
                    body = bytes(content)
                    raw_body_source = "response.content"
                elif isinstance(content, str):
                    body = content.encode("utf-8", errors="strict")
                    raw_body_source = "response.content_text"
            except Exception:
                body = None
            if body is None:
                try:
                    text = getattr(response, "text", None)
                    if isinstance(text, str):
                        body = text.encode("utf-8", errors="strict")
                        raw_body_source = "response.text"
                except Exception:
                    body = None

        if body is None:
            body_obj = getattr(exc, "response_json", None)
            if body_obj is not None:
                try:
                    body = json.dumps(body_obj, ensure_ascii=False, default=str).encode("utf-8")
                    raw_body_source = "sdk.response_json"
                except Exception:
                    body = None
        if body is None:
            body_obj = getattr(exc, "details", None)
            if body_obj is not None:
                try:
                    body = json.dumps(body_obj, ensure_ascii=False, default=str).encode("utf-8")
                    raw_body_source = "sdk.details"
                except Exception:
                    body = None
        if body is None:
            body = str(exc).encode("utf-8", errors="replace")
            raw_body_source = "exception_string"

        request_method = None
        request_url = None
        request = getattr(response, "request", None) if response is not None else None
        if request is not None:
            try:
                method = getattr(request, "method", None)
                request_method = str(method) if method not in (None, "") else None
            except Exception:
                request_method = None
            try:
                url = getattr(request, "url", None)
                request_url = str(url) if url not in (None, "") else None
            except Exception:
                request_url = None

        if status_code is not None:
            return ProviderHTTPError(
                status_code,
                body,
                content_type=lowered.get("content-type"),
                content_encoding=lowered.get("content-encoding"),
                request_id=request_id,
                response_headers=headers,
                phase=phase,
                request_method=request_method,
                request_url=request_url,
                raw_body_source=raw_body_source,
                upstream_exception_type=type(exc).__name__,
            )
        return ProviderRequestError("GEMINI_GENAI_SDK_ERROR", f"{phase}: {exc}")

# Historical import name, one implementation.
GeminiAIHubMixGenAIClient = GeminiGenAIClient

mount_continuation_handle = GeminiGenAIClient.mount_continuation_handle
extract_cache_facts = GeminiGenAIClient.extract_cache_facts
extract_session_facts = GeminiGenAIClient.extract_session_facts
