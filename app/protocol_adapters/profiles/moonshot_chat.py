from __future__ import annotations

import json
from typing import Any

import httpx

from app.protocol_adapters.base import ProviderHTTPError, ProviderRequestError
from app.protocol_adapters.wire_projection.http_wire import read_raw_response, decode_entity
from app.protocol_adapters.v2_base import SealedDispatchBundle, StandardizedProviderResult
from app.observability.http_wire_logger import normalization_active
from app.runtime.transport_guard import provider_transport
from app.core.context_codec import source_markers
from app.config import Settings
from app.core.idempotency import CANONICAL_REQUEST_VERSIONS
from app.materials.resolver import MaterialResolver
from app.canonical_schema import resolve_structured_output
from app.v2_repository import RelayV2Repository


class MoonshotChatAdapter:
    """Official Moonshot/Kimi Chat Completions adapter.

    Relay material identities remain provider-neutral. Text documents use the
    provider-derived file-extract artifact, while image/video inputs reuse the
    frozen official Files API ms:// binding. Raw input bytes are not uploaded here.
    """

    adapter_version = "moonshot-chat/5"
    _PROTECTED = {"model", "messages", "response_format", "stream", "reasoning_effort", "thinking"}

    def __init__(
        self,
        settings: Settings,
        materials: MaterialResolver,
        repo: RelayV2Repository,
        *, connection=None,
    ) -> None:
        if connection is None:
            from app.compatibility.connection_config import legacy_runtime
            connection = legacy_runtime(settings, "moonshot_chat")
        self.connection = connection
        self.settings = settings
        self.materials = materials
        self.repo = repo

    async def execute(self, context: SealedDispatchBundle) -> StandardizedProviderResult:
        context.verify_integrity()
        if not self.connection.secret:
            raise ProviderRequestError(
                "CONNECTION_NOT_CONFIGURED",
                "moonshot_official is enabled but MOONSHOT_API_KEY is not configured",
            )

        messages: list[dict[str, Any]] = []
        if (context.session.get("context_policy") == "conversation"
                and context.snapshot.get("history_mode", context.session.get("history_mode", "cumulative")) != "ephemeral_turn"):
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

        material_system_messages, material_parts = await self._material_parts(context)
        messages.extend(material_system_messages)
        user_message = self._user_message(context.snapshot.get("input"), material_parts)
        messages.append(user_message)

        payload: dict[str, Any] = {
            "model": context.snapshot.get("upstream_model") or context.snapshot["model"],
            "messages": messages,
            "stream": False,
        }
        self._apply_model_options(payload, context.snapshot)

        spec = resolve_structured_output(
            context.snapshot,
            fallback_name="structured_output" if normalization_active() else str(context.snapshot.get("metadata", {}).get("stage") or "structured_output"),
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

        url = self.connection.api_url("chat/completions")
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=None,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        headers = self._headers()
        async with httpx.AsyncClient(timeout=timeout, verify=True, transport=provider_transport(self.settings, "moonshot", url=url, connection=self.connection)) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                raw = await read_raw_response(response, log_context={"request_id": context.request_id, "session_id": context.session_id, "provider": "kimi", "connection_id": context.snapshot.get("connection_id"), "model": context.snapshot.get("model")})
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
        return StandardizedProviderResult(
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
            http_status=response_status,
            provider_request_id=self._request_id(response_headers),
            observed=observed_response_facts(response_headers, body_model=data.get("model"), protocol="moonshot_chat",
                channel_id=context.snapshot.get("channel_id") or self.connection.spec.channel_id,
                evidence_headers=(context.snapshot.get("protocol_profile") or {}).get("metadata", {}).get("response_evidence_headers")),
        )

    async def _material_parts(
        self, context: SealedDispatchBundle
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        system_messages: list[dict[str, Any]] = []
        visual_parts: list[dict[str, Any]] = []
        by_id = {str(x.get("material_id")): x for x in context.material_bindings}
        for material_id in context.material_ids:
            binding = by_id.get(material_id)
            if not binding:
                raise ProviderRequestError(
                    "MATERIAL_BINDING_MISSING",
                    f"Frozen Kimi binding missing for {material_id}",
                )
            filename = str(binding.get("filename") or material_id)
            purpose = str(binding.get("purpose") or "")
            if binding.get("projection_object_id"):
                raw = await self.materials.read_bound_text(binding, tenant_id=context.tenant_id,
                                                          conversation_hash=context.conversation_hash)
                begin, end = source_markers(material_id, filename, upgraded=True)
                system_messages.append({"role": "system", "content": f"{begin}\n{raw.decode('utf-8')}\n{end}"})
                continue
            if purpose == "file-extract":
                metadata = binding.get("metadata") if isinstance(binding.get("metadata"), dict) else {}
                extraction_object_id = metadata.get("extraction_object_id")
                if not extraction_object_id:
                    raise ProviderRequestError(
                        "MATERIAL_BINDING_INVALID",
                        f"Kimi text binding has no extraction artifact for {material_id}",
                    )
                if context.snapshot.get("schema_version") == "relay-request/2.4":
                    frozen_text = {**binding, "object_id": extraction_object_id,
                        "content_sha256": metadata.get("extraction_sha256")}
                    resolved = await self.materials.resolve_frozen(frozen_text, tenant_id=context.tenant_id,
                        conversation_hash=context.conversation_hash, with_bytes=True)
                    raw = resolved.data
                else:
                    raw = await self.materials.read_object_id(str(extraction_object_id))
                text = raw.decode("utf-8", errors="strict")
                begin, end = source_markers(material_id, filename, upgraded=normalization_active())
                system_messages.append(
                    {
                        "role": "system",
                        "content": f"{begin}\n{text}\n{end}",
                    }
                )
                continue

            uri = str(binding.get("external_uri") or "")
            if not uri.startswith("ms://"):
                raise ProviderRequestError(
                    "MATERIAL_BINDING_INVALID",
                    f"Kimi visual binding has no ms:// URI for {material_id}",
                )
            if purpose == "image":
                visual_parts.append({"type": "image_url", "image_url": {"url": uri}})
            elif purpose == "video":
                visual_parts.append({"type": "video_url", "video_url": {"url": uri}})
            else:
                raise ProviderRequestError(
                    "MATERIAL_REPRESENTATION_UNSUPPORTED",
                    f"Unsupported Kimi file purpose {purpose!r} for {filename}",
                )
        return system_messages, visual_parts

    @classmethod
    def _apply_model_options(cls, payload: dict[str, Any], snapshot: dict[str, Any]) -> None:
        if str(snapshot.get("schema_version") or "") in CANONICAL_REQUEST_VERSIONS:
            options = snapshot.get("effective_options") or {}
            if isinstance(options, dict):
                if "temperature" in options:
                    payload["temperature"] = options["temperature"]
                if "top_p" in options:
                    payload["top_p"] = options["top_p"]
                if "max_output_tokens" in options:
                    payload["max_tokens"] = options["max_output_tokens"]

            think_level = str(snapshot.get("think_level") or "auto").strip().lower() or "auto"
            model = str(snapshot.get("model") or "").strip().lower()

            # K2.7 Code is always-thinking. Relay normalizes every caller depth
            # to effective ``on`` and the native Kimi wire explicitly enables
            # Thinking using thinking.type, rather than inventing an effort.
            if cls._is_kimi_k27_code(model):
                payload["thinking"] = {"type": "enabled"}
                return

            if think_level in {"low", "high", "max"}:
                if not cls._supports_reasoning_effort(model):
                    raise ProviderRequestError(
                        "THINK_LEVEL_UNSUPPORTED",
                        f"think_level={think_level!r} has no Kimi reasoning_effort wire mapping for model {model!r}",
                    )
                payload["reasoning_effort"] = think_level
            elif think_level != "auto":
                raise ProviderRequestError(
                    "THINK_LEVEL_UNSUPPORTED",
                    f"Unsupported Kimi think_level={think_level!r}",
                )
            return

        provider_payload = snapshot.get("provider_payload") or {}
        if isinstance(provider_payload, dict):
            for key, value in provider_payload.items():
                if key not in cls._PROTECTED and key != "material_mode":
                    payload[key] = value

    @staticmethod
    def _is_kimi_k27_code(model: str) -> bool:
        return str(model or "").strip().lower().startswith("kimi-k2.7-code")

    @staticmethod
    def _supports_reasoning_effort(model: str) -> bool:
        return str(model or "").strip().lower().startswith("kimi-k3")

    def _headers(self) -> dict[str, str]:
        return {
            **self.connection.headers(),
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
