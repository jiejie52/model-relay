from __future__ import annotations

import base64
import io
import json
from typing import Any

from ..config import Settings
from .base import ProviderHTTPError, ProviderRequestError


class GeminiAIHubMixGenAIClient:
    """Small testable wrapper around the official ``google-genai`` SDK.

    New Gemini cache layouts use the SDK for Files + CachedContent lifecycle.
    Relay still owns all durable resource identities, fencing and retry policy.
    The in-memory File registry is only an optimization: async Worker recovery
    always rehydrates a Provider File via ``files.get`` when needed.
    """

    adapter_version = "google-genai-aihubmix/1"

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        types_module: Any | None = None,
    ) -> None:
        if settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required for Gemini google-genai SDK")
        self.settings = settings
        self.base_url = settings.aihubmix_gemini_sdk_base_url.rstrip("/")
        self._client = client
        self._types_module = types_module
        self._files_by_name: dict[str, Any] = {}
        self._files_by_uri: dict[str, Any] = {}

    def _types(self) -> Any:
        if self._types_module is not None:
            return self._types_module
        try:
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError(
                "google-genai is required for the current AIHubMix Gemini cache layout"
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
                "google-genai is required for the current AIHubMix Gemini cache layout"
            ) from exc
        types = self._types()

        # The current SDK calls this field ``base_url``. Keep retries explicit:
        # cache/file side effects are fenced and reconciled by Relay, not by an
        # opaque SDK retry loop.
        http_options = types.HttpOptions(
            base_url=self.base_url,
            api_version="v1beta",
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        self._client = genai.Client(
            api_key=self.settings.aihubmix_api_key.get_secret_value(),
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
            file_obj = await self.client.aio.files.upload(
                file=stream,
                config=types.UploadFileConfig(
                    display_name=filename,
                    mime_type=mime_type,
                ),
            )
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_files_upload") from exc
        return self.remember_file(file_obj)

    async def get_file(self, *, name: str, refresh: bool = False) -> Any:
        cached = self.cached_file(name=name)
        if cached is not None and not refresh:
            return cached
        try:
            file_obj = await self.client.aio.files.get(name=name)
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_files_get") from exc
        return self.remember_file(file_obj)

    async def delete_file(self, *, name: str) -> None:
        try:
            await self.client.aio.files.delete(name=name)
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
            cache = await self.client.aio.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    display_name=display_name,
                    contents=contents,
                    system_instruction=system_instruction,
                    ttl=f"{int(ttl_seconds)}s",
                ),
            )
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_cache_create") from exc
        return cache

    async def get_cache(self, *, name: str) -> Any:
        try:
            return await self.client.aio.caches.get(name=name)
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_cache_get") from exc

    async def renew_cache(self, *, name: str, expire_at: str) -> Any:
        types = self._types()
        try:
            return await self.client.aio.caches.update(
                name=name,
                config=types.UpdateCachedContentConfig(expire_time=expire_at),
            )
        except Exception as exc:
            raise self._provider_error(exc, phase="gemini_cache_renew") from exc

    async def delete_cache(self, *, name: str) -> None:
        try:
            await self.client.aio.caches.delete(name=name)
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

            if role == "user":
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
        sdk_parts = []
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                sdk_parts.append(types_module.Part.from_text(text=str(part.get("text") or "")))
        if not sdk_parts:
            return None
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

        body_obj = getattr(exc, "response_json", None)
        if body_obj is None:
            body_obj = getattr(exc, "details", None)
        if body_obj is None:
            body_obj = {"error": str(exc)}
        try:
            body = json.dumps(body_obj, ensure_ascii=False, default=str).encode("utf-8")
        except Exception:
            body = str(exc).encode("utf-8", errors="replace")

        if status_code is not None:
            return ProviderHTTPError(
                status_code,
                body,
                content_type=headers.get("content-type"),
                request_id=request_id,
                response_headers=headers,
                phase=phase,
            )
        return ProviderRequestError("GEMINI_GENAI_SDK_ERROR", f"{phase}: {exc}")
