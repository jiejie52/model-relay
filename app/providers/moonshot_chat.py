from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from .base import ProviderHTTPError, ProviderRequestError
from .http_wire import read_raw_response, decode_entity
from .v2_base import V2ExecutionContext, V2ProviderResult
from ..config import Settings
from ..materials.resolver import MaterialResolver
from ..structured_output import resolve_structured_output
from ..v2_repository import RelayV2Repository


class MoonshotChatAdapter:
    """Official Moonshot/Kimi Chat Completions adapter.

    Relay material identities remain provider-neutral. Text documents are adapted
    through Moonshot's file-extract flow and images are sent as Base64 data URLs;
    these are wire adaptations only and never replace the persisted Material.
    """

    adapter_version = "moonshot-chat/1"
    _PROTECTED = {"model", "messages", "response_format", "stream"}

    def __init__(
        self,
        settings: Settings,
        materials: MaterialResolver,
        repo: RelayV2Repository,
    ) -> None:
        self.settings = settings
        self.materials = materials
        self.repo = repo

    async def execute(self, context: V2ExecutionContext) -> V2ProviderResult:
        if self.settings.moonshot_api_key is None:
            raise ProviderRequestError(
                "CONNECTION_NOT_CONFIGURED",
                "moonshot_official is enabled but MOONSHOT_API_KEY is not configured",
            )

        messages: list[dict[str, Any]] = []
        if context.session.get("context_policy") == "conversation":
            for turn in context.history:
                transport = turn.get("transport_history") if isinstance(turn, dict) else None
                if not isinstance(transport, dict) or transport.get("kind") != "moonshot_chat":
                    continue
                user = transport.get("user_message")
                assistant = transport.get("assistant_message")
                if isinstance(user, dict):
                    messages.append(user)
                if isinstance(assistant, dict):
                    messages.append(assistant)

        instructions = context.snapshot.get("instructions")
        if instructions:
            messages.insert(0, {"role": "system", "content": str(instructions)})

        material_parts = await self._material_parts(context)
        user_message = self._user_message(context.snapshot.get("input"), material_parts)
        messages.append(user_message)

        payload: dict[str, Any] = {
            "model": context.snapshot["model"],
            "messages": messages,
            "stream": False,
        }
        provider_payload = context.snapshot.get("provider_payload") or {}
        if isinstance(provider_payload, dict):
            for key, value in provider_payload.items():
                if key not in self._PROTECTED and key != "material_mode":
                    payload[key] = value

        spec = resolve_structured_output(
            context.snapshot,
            fallback_name=str(context.snapshot.get("metadata", {}).get("stage") or "structured_output"),
        )
        if spec is not None:
            if spec.mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            elif spec.mode == "json_schema" and spec.schema is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": spec.name or "structured_output",
                        "schema": spec.schema,
                        "strict": bool(spec.strict),
                    },
                }

        url = f"{self.settings.moonshot_root}/chat/completions"
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=None,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        headers = self._headers()
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                raw = await read_raw_response(response)
                response_status = response.status_code
                response_headers = dict(response.headers)

        if not 200 <= response_status < 300:
            raise ProviderHTTPError(
                response_status,
                raw,
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
            )
        try:
            decoded = decode_entity(raw, response_headers.get("content-encoding"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            raise ProviderHTTPError(
                response_status,
                raw,
                "Moonshot success response was not valid JSON",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
            ) from exc

        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise ProviderHTTPError(
                response_status,
                raw,
                "Moonshot response has no assistant message",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
            )
        text = self._message_text(message)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cached_tokens = None
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens") is not None:
            try:
                cached_tokens = int(prompt_details["cached_tokens"])
            except Exception:
                cached_tokens = None

        assistant_message = dict(message)
        assistant_message.setdefault("role", "assistant")
        return V2ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=str(data.get("id") or "") or None,
            usage=usage,
            cached_tokens=cached_tokens,
            response_output=assistant_message,
            history_entry={
                "transport_history": {
                    "kind": "moonshot_chat",
                    "user_message": user_message,
                    "assistant_message": assistant_message,
                }
            },
        )

    async def _material_parts(self, context: V2ExecutionContext) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for material_id in context.material_ids:
            resolved = await self.materials.resolve(
                material_id,
                tenant_id=context.tenant_id,
                conversation_hash=context.conversation_hash,
                with_bytes=True,
            )
            assert resolved.data is not None
            mime = str(resolved.material.get("content_type") or "application/octet-stream")
            filename = str(resolved.material.get("filename") or material_id)

            if mime.startswith("image/"):
                encoded = base64.b64encode(resolved.data).decode("ascii")
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"},
                    }
                )
                continue

            if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
                text = resolved.data.decode("utf-8", errors="replace")
            else:
                material_mode = str((context.snapshot.get("provider_payload") or {}).get("material_mode") or "text").lower()
                if material_mode in {"vision", "visual", "native_visual"}:
                    raise ProviderRequestError(
                        "MATERIAL_REPRESENTATION_UNSUPPORTED",
                        f"Moonshot text file-extract would discard visual/layout evidence for {filename}; a verified visual representation is required",
                    )
                text = await self._extract_document_text(
                    material_id=material_id,
                    filename=filename,
                    mime=mime,
                    data=resolved.data,
                    connection_id=context.snapshot["connection_id"],
                )
            parts.append(
                {
                    "type": "text",
                    "text": f"[material {material_id}: {filename}]\n{text}",
                }
            )
        return parts

    async def _extract_document_text(
        self,
        *,
        material_id: str,
        filename: str,
        mime: str,
        data: bytes,
        connection_id: str,
    ) -> str:
        existing = await self.repo.get_provider_binding(
            material_id=material_id,
            connection_id=connection_id,
            purpose="file-extract",
            representation="text",
            adapter_version=self.adapter_version,
        )
        provider_file_id = existing.get("provider_file_id") if existing else None
        if not provider_file_id:
            provider_file_id = await self._upload_file(filename, mime, data)
            await self.repo.upsert_provider_binding(
                {
                    "material_id": material_id,
                    "connection_id": connection_id,
                    "purpose": "file-extract",
                    "representation": "text",
                    "adapter_version": self.adapter_version,
                    "provider_file_id": provider_file_id,
                    "metadata": {},
                }
            )
        try:
            return await self._read_file_content(str(provider_file_id))
        except ProviderHTTPError as exc:
            if exc.status_code not in {404, 410}:
                raise
            # Provider binding is a disposable cache. Rebuild it from the
            # authoritative Relay Material when the provider-side file expires.
            provider_file_id = await self._upload_file(filename, mime, data)
            await self.repo.upsert_provider_binding(
                {
                    "material_id": material_id,
                    "connection_id": connection_id,
                    "purpose": "file-extract",
                    "representation": "text",
                    "adapter_version": self.adapter_version,
                    "provider_file_id": provider_file_id,
                    "metadata": {"recreated": True},
                }
            )
            return await self._read_file_content(str(provider_file_id))

    async def _upload_file(self, filename: str, mime: str, data: bytes) -> str:
        url = f"{self.settings.moonshot_root}/files"
        headers = {"Authorization": f"Bearer {self.settings.moonshot_api_key.get_secret_value()}"}
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                data={"purpose": "file-extract"},
                files={"file": (filename, data, mime)},
            ) as response:
                raw = await read_raw_response(response)
                response_status = response.status_code
                response_headers = dict(response.headers)
        if not 200 <= response_status < 300:
            raise ProviderHTTPError(
                response_status,
                raw,
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
            )
        try:
            decoded = decode_entity(raw, response_headers.get("content-encoding"))
            payload = json.loads(decoded.decode("utf-8"))
            file_id = payload.get("id")
        except Exception as exc:
            raise ProviderHTTPError(response_status, raw, "Moonshot file upload returned invalid JSON", content_type=response_headers.get("content-type"), content_encoding=response_headers.get("content-encoding"), request_id=self._request_id(response_headers)) from exc
        if not file_id:
            raise ProviderRequestError("PROVIDER_FILE_BINDING", "Moonshot file upload returned no file id")
        return str(file_id)

    async def _read_file_content(self, file_id: str) -> str:
        url = f"{self.settings.moonshot_root}/files/{file_id}/content"
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            async with client.stream("GET", url, headers=self._headers()) as response:
                raw = await read_raw_response(response)
                response_status = response.status_code
                response_headers = dict(response.headers)
        if not 200 <= response_status < 300:
            raise ProviderHTTPError(
                response_status,
                raw,
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
            )
        decoded = decode_entity(raw, response_headers.get("content-encoding"))
        content_type = str(response_headers.get("content-type") or "")
        if "json" in content_type:
            try:
                obj = json.loads(decoded.decode("utf-8"))
                if isinstance(obj, dict):
                    for key in ("content", "text", "data"):
                        if isinstance(obj.get(key), str):
                            return obj[key]
            except Exception:
                pass
        return decoded.decode("utf-8", errors="replace")

    def _headers(self) -> dict[str, str]:
        assert self.settings.moonshot_api_key is not None
        return {
            "Authorization": f"Bearer {self.settings.moonshot_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

    @staticmethod
    def _request_id(headers: Any) -> str | None:
        for key in ("x-request-id", "request-id", "x-moonshot-request-id"):
            if headers.get(key):
                return headers.get(key)
        return None

    @staticmethod
    def _user_message(value: Any, material_parts: list[dict[str, Any]]) -> dict[str, Any]:
        if isinstance(value, str):
            query_text = value
        else:
            query_text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if not material_parts:
            return {"role": "user", "content": query_text}
        content = list(material_parts)
        content.append({"type": "text", "text": query_text})
        return {"role": "user", "content": content}

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
            return "\n".join(pieces)
        return ""
