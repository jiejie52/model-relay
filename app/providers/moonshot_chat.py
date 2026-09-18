from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import httpx

from ..config import Settings
from ..storage_paths import provider_derived_material_path
from ..structured_output import StructuredOutputError, resolve_structured_output
from ..utils import utcnow
from .base import ProviderRequestError, ProviderResult
from .v2_common import decode_http_body, response_http_error, send_raw, transport_error, wire_hash


class MoonshotChatCompletionsAdapter:
    provider = "moonshot"
    protocol = "chat_completions"
    history_codec = "moonshot-chat-native"
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
        messages: list[dict[str, Any]] = []
        for message in context:
            messages.append(await self._encode_message(message, material_resolver, session))
        if isinstance(history, list):
            for item in history:
                if isinstance(item, dict):
                    messages.append(item)
        current_messages = []
        for message in request_snapshot.get("input") or []:
            encoded = await self._encode_message(message, material_resolver, session)
            messages.append(encoded)
            current_messages.append(encoded)

        payload: dict[str, Any] = {"model": model, "messages": messages}
        applied_generation = self._apply_generation(payload, request_snapshot.get("generation") or {}, model)
        self._apply_structured_output(payload, request_snapshot)
        h = wire_hash(payload)
        await before_dispatch(h)

        assert self.settings.moonshot_api_key is not None
        headers = {
            "Authorization": f"Bearer {self.settings.moonshot_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        url = f"{self.profile.api_origin.rstrip('/')}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=True, follow_redirects=False) as client:
                response, raw = await send_raw(client, "POST", url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise transport_error(exc, provider=self.provider, service="chat_completions") from exc
        if not 200 <= response.status_code < 300:
            raise response_http_error(response, raw, provider=self.provider, service="chat_completions")
        try:
            decoded = decode_http_body(raw, response.headers.get("content-encoding"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            from .base import ProviderHTTPError
            raise ProviderHTTPError(
                response.status_code,
                raw,
                "Moonshot response was not valid JSON",
                headers=list(response.headers.multi_items()),
                content_type=response.headers.get("content-type"),
                content_encoding=response.headers.get("content-encoding"),
                provider=self.provider,
                service="chat_completions",
            ) from exc

        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        assistant = choice.get("message") if isinstance(choice.get("message"), dict) else {"role": "assistant", "content": ""}
        text = assistant.get("content") if isinstance(assistant.get("content"), str) else ""
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        finish_reason = str(choice.get("finish_reason")) if choice.get("finish_reason") is not None else None

        # Native history must preserve the full assistant message, including
        # reasoning_content/tool_calls/tool_call_id and unknown extension fields.
        history_delta = [*current_messages, assistant]
        return ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=data.get("id"),
            usage=usage,
            cached_tokens=self._cached_tokens(usage),
            response_output=[],
            history_delta=history_delta,
            finish_reason=finish_reason,
            result_type="tool_calls" if assistant.get("tool_calls") else "message",
            wire_request_hash=h,
            applied_generation=applied_generation,
            provider_metadata={"assistant_message": assistant},
        )

    def decode_archived_v2(self, request_snapshot: dict[str, Any], archived: dict[str, Any]) -> ProviderResult:
        return ProviderResult(**archived)

    async def _encode_message(self, message: dict[str, Any], resolver: Any, session: dict[str, Any]) -> dict[str, Any]:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, str):
            return {"role": role, "content": content}
        if not isinstance(content, list):
            return {"role": role, "content": str(content or "")}

        parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                parts.append({"type": "text", "text": str(part)})
                continue
            ptype = str(part.get("type") or "")
            if ptype == "text":
                parts.append({"type": "text", "text": str(part.get("text") or "")})
                continue
            if ptype != "material_ref":
                # Generic chat content is kept if already provider-neutral text-like.
                if "text" in part:
                    parts.append({"type": "text", "text": str(part.get("text") or "")})
                else:
                    raise ProviderRequestError("MOONSHOT_CONTENT_UNSUPPORTED", f"Unsupported content part: {ptype or '<empty>'}")
                continue

            material_id = str(part.get("material_id") or "")
            metadata = await resolver.metadata(material_id)
            mime = str(metadata.get("detected_mime") or metadata.get("declared_mime") or "application/octet-stream").lower()
            source_ref = str(part.get("source_ref") or metadata.get("filename") or material_id)
            if mime.startswith("image/"):
                raw = await resolver.read_bytes(material_id)
                encoded = base64.b64encode(raw).decode("ascii")
                parts.append({"type": "text", "text": f"[SOURCE_FILE:{source_ref}]"})
                parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
                parts.append({"type": "text", "text": f"[END_SOURCE_FILE:{source_ref}]"})
            elif mime in {"text/plain", "text/markdown"} or str(metadata.get("filename", "")).lower().endswith((".txt", ".md")):
                raw = await resolver.read_bytes(material_id)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProviderRequestError("MATERIAL_ENCODING_UNSUPPORTED", f"{source_ref} is not valid UTF-8") from exc
                parts.append({"type": "text", "text": f"[SOURCE_FILE:{source_ref}]\n{text}\n[END_SOURCE_FILE:{source_ref}]"})
            elif mime == "application/pdf":
                extracted = await self._extract_file_text(metadata, resolver, session)
                parts.append({"type": "text", "text": f"[SOURCE_FILE:{source_ref}]\n{extracted}\n[END_SOURCE_FILE:{source_ref}]"})
            else:
                raise ProviderRequestError("MOONSHOT_MATERIAL_UNSUPPORTED", f"Moonshot profile cannot consume {mime} for {source_ref}")

        # Chat Completions accepts a string for text-only messages and a content
        # array for multimodal messages. Keep arrays so file boundaries remain explicit.
        return {"role": role, "content": parts}

    async def _extract_file_text(self, material: dict[str, Any], resolver: Any, session: dict[str, Any]) -> str:
        material_id = str(material["id"])
        profile_name = str(session["upstream_profile"])
        account_scope = str(session["account_scope"])
        transform_version = "moonshot-file-extract-v1"
        binding = await resolver.repository.get_material_binding(
            material_id=material_id,
            provider=self.provider,
            upstream_profile=profile_name,
            account_scope=account_scope,
            purpose="file-extract",
            transform_version=transform_version,
        )
        if binding and binding.get("status") == "ready" and binding.get("derived_object_path"):
            raw = await resolver.repository.backend.storage_get(binding["derived_object_path"])
            return raw.decode("utf-8")

        try:
            binding_claim = await resolver.claim_binding(
                material=material,
                provider=self.provider,
                upstream_profile=profile_name,
                account_scope=account_scope,
                protocol=self.protocol,
                capability_group="moonshot-file-extract",
                purpose="file-extract",
                transform_version=transform_version,
                lease_seconds=max(300, int(self.settings.worker_max_runtime_seconds)),
            )
        except TimeoutError as exc:
            raise ProviderRequestError("PROVIDER_BINDING_BUSY", str(exc)) from exc
        except ValueError as exc:
            raise ProviderRequestError("PROVIDER_BINDING_RESERVATION_FAILED", str(exc)) from exc

        try:
            raw_file = await resolver.read_bytes(material_id)
            filename = str(material.get("filename") or f"{material_id}.pdf")
            mime = str(material.get("detected_mime") or material.get("declared_mime") or "application/octet-stream")
            assert self.settings.moonshot_api_key is not None
            auth = {"Authorization": f"Bearer {self.settings.moonshot_api_key.get_secret_value()}"}
            timeout = httpx.Timeout(
                connect=self.settings.upstream_connect_timeout_seconds,
                read=self.settings.upstream_read_timeout_seconds,
                write=self.settings.upstream_write_timeout_seconds,
                pool=self.settings.upstream_pool_timeout_seconds,
            )
            root = self.profile.api_origin.rstrip("/")
            try:
                async with httpx.AsyncClient(timeout=timeout, verify=True, follow_redirects=False) as client:
                    upload, upload_raw = await send_raw(
                        client,
                        "POST",
                        f"{root}/files",
                        headers={**auth, "Accept-Encoding": "identity"},
                        data={"purpose": "file-extract"},
                        files={"file": (filename, raw_file, mime)},
                    )
                    if not 200 <= upload.status_code < 300:
                        raise response_http_error(upload, upload_raw, provider=self.provider, service="files")
                    try:
                        upload_obj = json.loads(decode_http_body(upload_raw, upload.headers.get("content-encoding")).decode("utf-8"))
                    except Exception as exc:
                        raise ProviderRequestError("MOONSHOT_FILE_UPLOAD_INVALID", "Moonshot file upload response was not JSON") from exc
                    native_id = str(upload_obj.get("id") or "")
                    if not native_id:
                        raise ProviderRequestError("MOONSHOT_FILE_ID_MISSING", "Moonshot file upload returned no file id")

                    content_response, content_raw = await send_raw(
                        client,
                        "GET",
                        f"{root}/files/{native_id}/content",
                        headers={**auth, "Accept-Encoding": "identity"},
                    )
                    if not 200 <= content_response.status_code < 300:
                        raise response_http_error(content_response, content_raw, provider=self.provider, service="file-content")
            except httpx.HTTPError as exc:
                raise transport_error(exc, provider=self.provider, service="files") from exc

            try:
                content_decoded = decode_http_body(content_raw, content_response.headers.get("content-encoding"))
                maybe_json = json.loads(content_decoded.decode("utf-8"))
                if isinstance(maybe_json, dict):
                    extracted = str(maybe_json.get("content") or maybe_json.get("text") or "")
                else:
                    extracted = str(maybe_json)
            except Exception:
                extracted = decode_http_body(content_raw, content_response.headers.get("content-encoding")).decode("utf-8", errors="strict")
            if not extracted:
                raise ProviderRequestError("MOONSHOT_FILE_EXTRACT_EMPTY", f"Moonshot returned empty extracted content for {filename}")

            derived_path = provider_derived_material_path(
                self.settings,
                str(material["tenant_id"]),
                str(material["conversation_hash"]),
                material_id,
                self.provider,
                int(material.get("generation") or 1),
                "file-extract.txt",
            )
            await resolver.repository.backend.storage_put(
                derived_path,
                extracted.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
            try:
                await resolver.complete_binding(
                    binding_claim,
                    native_file_id=native_id,
                    native_uri=None,
                    derived_object_path=derived_path,
                    expires_at=None,
                    transport_epoch=None,
                    wire_request_hash=None,
                    cache_identity=None,
                )
            except ValueError as exc:
                raise ProviderRequestError("PROVIDER_BINDING_FENCE_LOST", str(exc)) from exc
            return extracted
        except Exception:
            await resolver.fail_binding(binding_claim)
            raise

    def _apply_generation(self, payload: dict[str, Any], generation: dict[str, Any], model: str) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        reasoning = generation.get("reasoning") if isinstance(generation.get("reasoning"), dict) else {}
        lower = model.lower()
        if lower.startswith("kimi-k3"):
            if reasoning.get("thinking") is not None or reasoning.get("mode") is not None:
                raise ProviderRequestError("MODEL_REASONING_CONFIG_INVALID", "Kimi K3 uses reasoning_effort, not thinking mode")
            effort = str(reasoning.get("effort") or "auto").lower()
            if effort != "auto":
                if effort not in {"low", "high", "max"}:
                    raise ProviderRequestError("MODEL_REASONING_CONFIG_INVALID", f"Unsupported Kimi K3 reasoning effort: {effort}")
                payload["reasoning_effort"] = effort
                applied["reasoning_effort"] = effort
        elif lower.startswith("kimi-k2.6"):
            if reasoning.get("effort") not in {None, "", "auto"}:
                raise ProviderRequestError("MODEL_REASONING_CONFIG_INVALID", "Kimi K2.6 does not use reasoning_effort")
            mode = str(reasoning.get("mode") or "auto").lower()
            if mode != "auto":
                if mode not in {"enabled", "disabled"}:
                    raise ProviderRequestError("MODEL_REASONING_CONFIG_INVALID", f"Unsupported Kimi K2.6 thinking mode: {mode}")
                thinking = {"type": mode}
                if "keep" in reasoning:
                    thinking["keep"] = bool(reasoning["keep"])
                payload["thinking"] = thinking
                applied["thinking"] = thinking
        elif reasoning:
            raise ProviderRequestError("MODEL_REASONING_CONFIG_INVALID", f"No reasoning mapping registered for {model}")

        # Keep the generic surface intentionally narrow. Fixed model parameters
        # must not be silently overwritten to satisfy provider constraints.
        for key in ("temperature", "top_p", "n"):
            if key in generation:
                raise ProviderRequestError("MODEL_PARAMETER_FIXED", f"{key} must be omitted for the registered Moonshot profile")
        if "max_tokens" in generation:
            payload["max_tokens"] = int(generation["max_tokens"])
            applied["max_tokens"] = int(generation["max_tokens"])
        return applied

    def _apply_structured_output(self, payload: dict[str, Any], request_snapshot: dict[str, Any]) -> None:
        try:
            spec = resolve_structured_output(request_snapshot, fallback_name="relay_output")
        except StructuredOutputError as exc:
            raise ProviderRequestError(exc.code, exc.message) from exc
        if spec is None:
            return
        if spec.mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif spec.mode == "json_schema" and spec.schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": spec.name or "relay_output",
                    "schema": spec.schema,
                    "strict": bool(spec.strict),
                },
            }
        else:
            raise ProviderRequestError("STRUCTURED_OUTPUT_MODE_UNSUPPORTED", f"Unsupported Moonshot structured output mode: {spec.mode}")

    @staticmethod
    def _cached_tokens(usage: dict[str, Any]) -> int | None:
        for candidate in (
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            (usage.get("input_tokens_details") or {}).get("cached_tokens"),
            usage.get("cached_tokens"),
        ):
            if isinstance(candidate, int):
                return candidate
        return None
