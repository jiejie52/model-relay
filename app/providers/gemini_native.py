from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from ..config import Settings
from ..structured_output import StructuredOutputError, project_schema_for_provider, resolve_structured_output
from ..utils import utcnow
from .base import ProviderRequestError, ProviderResult
from .v2_common import decode_http_body, parse_expiry, response_http_error, send_raw, transport_error, wire_hash


class GeminiNativeAdapter:
    provider = "gemini"
    protocol = "generate_content"
    history_codec = "gemini-native"
    history_codec_version = "1"

    def __init__(self, settings: Settings, profile: Any) -> None:
        self.settings = settings
        self.profile = profile

    async def execute_v2(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any],
        context: list[dict[str, Any]],
        history: Any,
        material_resolver: Any,
        before_dispatch,
    ) -> ProviderResult:
        model = str(request_snapshot["model"])
        contents: list[dict[str, Any]] = []
        for message in context:
            contents.append(await self._encode_message(message, material_resolver, session))
        if isinstance(history, list):
            contents.extend(x for x in history if isinstance(x, dict))
        current: list[dict[str, Any]] = []
        for message in request_snapshot.get("input") or []:
            encoded = await self._encode_message(message, material_resolver, session)
            contents.append(encoded)
            current.append(encoded)

        payload: dict[str, Any] = {"contents": contents}
        generation_config: dict[str, Any] = {}
        generation = request_snapshot.get("generation") or {}
        for source, target in (
            ("temperature", "temperature"),
            ("top_p", "topP"),
            ("max_output_tokens", "maxOutputTokens"),
        ):
            if source in generation:
                generation_config[target] = generation[source]
        reasoning = generation.get("reasoning") if isinstance(generation.get("reasoning"), dict) else {}
        if reasoning and str(reasoning.get("effort") or "auto").lower() != "auto":
            raise ProviderRequestError("MODEL_REASONING_CONFIG_INVALID", "Native Gemini profile has no generic reasoning-effort mapping registered")
        self._apply_structured_output(generation_config, request_snapshot, model)
        if generation_config:
            payload["generationConfig"] = generation_config
        h = wire_hash(payload)
        await before_dispatch(h)

        try:
            binding_claim = await resolver.claim_binding(
                material=material,
                provider=self.provider,
                upstream_profile=str(session["upstream_profile"]),
                account_scope=str(session["account_scope"]),
                protocol=self.protocol,
                capability_group="gemini-files",
                purpose="gemini-file",
                transform_version="gemini-files-v1",
                lease_seconds=max(300, int(self.settings.worker_max_runtime_seconds)),
            )
        except TimeoutError as exc:
            raise ProviderRequestError("PROVIDER_BINDING_BUSY", str(exc)) from exc
        except ValueError as exc:
            raise ProviderRequestError("PROVIDER_BINDING_RESERVATION_FAILED", str(exc)) from exc
        if self.settings.gemini_api_key is None:
            await resolver.fail_binding(binding_claim)
            raise ProviderRequestError("PROVIDER_CREDENTIAL_MISSING", "GEMINI_API_KEY is not configured")
        key = self.settings.gemini_api_key.get_secret_value()
        url = f"{self.profile.api_origin.rstrip('/')}/models/{model}:generateContent?key={key}"
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=True, follow_redirects=False) as client:
                response, raw = await send_raw(
                    client,
                    "POST",
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"},
                )
        except httpx.HTTPError as exc:
            raise transport_error(exc, provider=self.provider, service="generateContent") from exc
        if not 200 <= response.status_code < 300:
            raise response_http_error(response, raw, provider=self.provider, service="generateContent")
        try:
            decoded = decode_http_body(raw, response.headers.get("content-encoding"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            from .base import ProviderHTTPError
            raise ProviderHTTPError(
                response.status_code,
                raw,
                "Gemini response was not valid JSON",
                headers=list(response.headers.multi_items()),
                content_type=response.headers.get("content-type"),
                content_encoding=response.headers.get("content-encoding"),
                provider=self.provider,
                service="generateContent",
            ) from exc

        candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
        candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {"role": "model", "parts": []}
        pieces: list[str] = []
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
        text = "\n".join(pieces)
        usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        cached = usage.get("cachedContentTokenCount") if isinstance(usage.get("cachedContentTokenCount"), int) else None
        return ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=data.get("responseId"),
            usage=usage,
            cached_tokens=cached,
            response_output=[],
            history_delta=[*current, content],
            finish_reason=str(candidate.get("finishReason")) if candidate.get("finishReason") is not None else None,
            result_type="message",
            wire_request_hash=h,
            applied_generation=generation_config,
            provider_metadata={"candidate": candidate},
        )

    def decode_archived_v2(self, request_snapshot: dict[str, Any], archived: dict[str, Any]) -> ProviderResult:
        return ProviderResult(**archived)

    async def _encode_message(self, message: dict[str, Any], resolver: Any, session: dict[str, Any]) -> dict[str, Any]:
        role = str(message.get("role") or "user")
        gemini_role = "model" if role == "assistant" else "user"
        content = message.get("content")
        parts: list[dict[str, Any]] = []
        if isinstance(content, str):
            parts.append({"text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    parts.append({"text": str(part)})
                    continue
                ptype = str(part.get("type") or "")
                if ptype == "text":
                    parts.append({"text": str(part.get("text") or "")})
                    continue
                if ptype != "material_ref":
                    if "text" in part:
                        parts.append({"text": str(part.get("text") or "")})
                        continue
                    raise ProviderRequestError("GEMINI_CONTENT_UNSUPPORTED", f"Unsupported content part: {ptype or '<empty>'}")
                material_id = str(part.get("material_id") or "")
                metadata = await resolver.metadata(material_id)
                source_ref = str(part.get("source_ref") or metadata.get("filename") or material_id)
                mime = str(metadata.get("detected_mime") or metadata.get("declared_mime") or "application/octet-stream")
                raw = await resolver.read_bytes(material_id)
                parts.append({"text": f"[SOURCE_FILE:{source_ref}]"})
                if len(raw) <= self.settings.material_inline_max_bytes:
                    parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}})
                else:
                    uri = await self._ensure_file_binding(metadata, raw, mime, resolver, session)
                    parts.append({"fileData": {"mimeType": mime, "fileUri": uri}})
                parts.append({"text": f"[END_SOURCE_FILE:{source_ref}]"})
        else:
            parts.append({"text": str(content or "")})
        return {"role": gemini_role, "parts": parts}

    async def _ensure_file_binding(self, material: dict[str, Any], raw: bytes, mime: str, resolver: Any, session: dict[str, Any]) -> str:
        binding = await resolver.repository.get_material_binding(
            material_id=str(material["id"]),
            provider=self.provider,
            upstream_profile=str(session["upstream_profile"]),
            account_scope=str(session["account_scope"]),
            purpose="gemini-file",
            transform_version="gemini-files-v1",
        )
        expiry = parse_expiry((binding or {}).get("expires_at"))
        if binding and binding.get("status") == "ready" and binding.get("native_uri") and (expiry is None or expiry > utcnow()):
            return str(binding["native_uri"])
        try:
            binding_claim = await resolver.claim_binding(
                material=material,
                provider=self.provider,
                upstream_profile=str(session["upstream_profile"]),
                account_scope=str(session["account_scope"]),
                protocol=self.protocol,
                capability_group="gemini-files",
                purpose="gemini-file",
                transform_version="gemini-files-v1",
                lease_seconds=max(300, int(self.settings.worker_max_runtime_seconds)),
            )
        except TimeoutError as exc:
            raise ProviderRequestError("PROVIDER_BINDING_BUSY", str(exc)) from exc
        except ValueError as exc:
            raise ProviderRequestError("PROVIDER_BINDING_RESERVATION_FAILED", str(exc)) from exc

        try:
            if self.settings.gemini_api_key is None:
                raise ProviderRequestError("PROVIDER_CREDENTIAL_MISSING", "GEMINI_API_KEY is not configured")
            key = self.settings.gemini_api_key.get_secret_value()
            upload_root = self.profile.api_origin.rstrip("/").replace("/v1beta", "/upload/v1beta")
            start_url = f"{upload_root}/files?key={key}"
            timeout = httpx.Timeout(
                connect=self.settings.upstream_connect_timeout_seconds,
                read=self.settings.upstream_read_timeout_seconds,
                write=self.settings.upstream_write_timeout_seconds,
                pool=self.settings.upstream_pool_timeout_seconds,
            )
            headers = {
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(len(raw)),
                "X-Goog-Upload-Header-Content-Type": mime,
                "Content-Type": "application/json",
            }
            try:
                async with httpx.AsyncClient(timeout=timeout, verify=True, follow_redirects=False) as client:
                    start, start_raw = await send_raw(
                        client,
                        "POST",
                        start_url,
                        headers={**headers, "Accept-Encoding": "identity"},
                        json={"file": {"display_name": str(material.get("filename") or material["id"])}},
                    )
                    if not 200 <= start.status_code < 300:
                        raise response_http_error(start, start_raw, provider=self.provider, service="files-start")
                    upload_url = start.headers.get("x-goog-upload-url")
                    if not upload_url:
                        raise ProviderRequestError("GEMINI_UPLOAD_URL_MISSING", "Gemini resumable upload returned no upload URL")
                    finish, finish_raw = await send_raw(
                        client,
                        "POST",
                        upload_url,
                        headers={
                            "Content-Length": str(len(raw)),
                            "X-Goog-Upload-Offset": "0",
                            "X-Goog-Upload-Command": "upload, finalize",
                            "Content-Type": mime,
                            "Accept-Encoding": "identity",
                        },
                        content=raw,
                    )
                    if not 200 <= finish.status_code < 300:
                        raise response_http_error(finish, finish_raw, provider=self.provider, service="files-upload")
            except httpx.HTTPError as exc:
                raise transport_error(exc, provider=self.provider, service="files") from exc
            try:
                obj = json.loads(decode_http_body(finish_raw, finish.headers.get("content-encoding")).decode("utf-8"))
            except Exception as exc:
                raise ProviderRequestError("GEMINI_FILE_RESPONSE_INVALID", "Gemini file upload response was not JSON") from exc
            file_obj = obj.get("file") if isinstance(obj.get("file"), dict) else obj
            uri = str(file_obj.get("uri") or "")
            if not uri:
                raise ProviderRequestError("GEMINI_FILE_URI_MISSING", "Gemini file upload returned no URI")
            try:
                await resolver.complete_binding(
                    binding_claim,
                    native_file_id=file_obj.get("name"),
                    native_uri=uri,
                    derived_object_path=None,
                    expires_at=file_obj.get("expirationTime"),
                    transport_epoch=None,
                    wire_request_hash=None,
                    cache_identity=None,
                )
            except ValueError as exc:
                raise ProviderRequestError("PROVIDER_BINDING_FENCE_LOST", str(exc)) from exc
            return uri
        except Exception:
            await resolver.fail_binding(binding_claim)
            raise

    def _apply_structured_output(self, generation_config: dict[str, Any], request_snapshot: dict[str, Any], model: str) -> None:
        try:
            spec = resolve_structured_output(request_snapshot, fallback_name="relay_output")
        except StructuredOutputError as exc:
            raise ProviderRequestError(exc.code, exc.message) from exc
        if spec is None:
            return
        generation_config["responseMimeType"] = "application/json"
        if spec.mode == "json_schema" and spec.schema:
            projection = project_schema_for_provider(spec.schema, provider="gemini", model=model)
            generation_config["responseSchema"] = projection.schema
        elif spec.mode != "json_object":
            raise ProviderRequestError("STRUCTURED_OUTPUT_MODE_UNSUPPORTED", f"Unsupported Gemini structured output mode: {spec.mode}")
