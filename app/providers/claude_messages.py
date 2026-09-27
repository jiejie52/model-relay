from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from .base import ProviderHTTPError, ProviderRequestError
from .http_wire import decode_entity, observed_response_facts, read_raw_response
from .v2_base import V2ExecutionContext, V2ProviderResult
from ..config import Settings
from ..materials.resolver import MaterialResolver
from ..structured_output import StructuredOutputError, resolve_structured_output


class ClaudeMessagesV2Adapter:
    """Reusable Anthropic Messages protocol adapter over a configured channel."""

    adapter_version = "claude-messages-v2/1"

    def __init__(
        self,
        settings: Settings,
        materials: MaterialResolver,
        *,
        base_url: str,
        api_key: str,
        connection_id: str,
        channel_id: str,
    ) -> None:
        self.settings = settings
        self.materials = materials
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.connection_id = connection_id
        self.channel_id = channel_id

    async def execute(self, context: V2ExecutionContext) -> V2ProviderResult:
        messages: list[dict[str, Any]] = []
        if context.session.get("context_policy") == "conversation":
            for turn in context.history:
                transport = turn.get("transport_history") if isinstance(turn, dict) else None
                if not isinstance(transport, dict) or transport.get("kind") != "claude_messages":
                    continue
                user_message = transport.get("user_message")
                assistant_message = transport.get("assistant_message")
                if isinstance(user_message, dict):
                    messages.append(user_message)
                if isinstance(assistant_message, dict):
                    messages.append(assistant_message)

        content = await self._user_content(context)
        user_message = {"role": "user", "content": content}
        messages.append(user_message)

        options = context.snapshot.get("effective_options") or {}
        if not isinstance(options, dict) or "max_output_tokens" not in options:
            raise ProviderRequestError(
                "OPTION_CONTRACT_INVALID",
                "Claude Messages requires the frozen capability contract to provide max_output_tokens",
            )
        payload: dict[str, Any] = {
            "model": str(context.snapshot["model"]),
            "max_tokens": int(options["max_output_tokens"]),
            "messages": messages,
        }
        instructions = context.snapshot.get("instructions")
        if instructions:
            payload["system"] = str(instructions)
        self._apply_thinking_and_output(payload, context.snapshot)

        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
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
                raw = await read_raw_response(
                    response,
                    log_context={
                        "request_id": context.request_id,
                        "session_id": context.session_id,
                        "provider": context.snapshot.get("provider"),
                        "connection_id": self.connection_id,
                        "model": context.snapshot.get("model"),
                    },
                )
                status = response.status_code
                response_headers = dict(response.headers)

        if not 200 <= status < 300:
            raise ProviderHTTPError(
                status,
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
                status,
                raw,
                "Claude Messages success response was not valid JSON",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
            ) from exc

        response_content = data.get("content") if isinstance(data.get("content"), list) else []
        if not response_content:
            raise ProviderHTTPError(
                status,
                raw,
                "Claude Messages response has no content blocks",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
            )
        assistant_message = {"role": "assistant", "content": response_content}
        text = "\n".join(
            str(block.get("text"))
            for block in response_content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cached = usage.get("cache_read_input_tokens")
        cached_tokens = int(cached) if isinstance(cached, int) else None
        return V2ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=str(data.get("id") or "") or None,
            usage=usage,
            cached_tokens=cached_tokens,
            response_output=response_content,
            history_entry={
                "transport_history": {
                    "kind": "claude_messages",
                    "user_message": user_message,
                    # Preserve full provider transport blocks (including any
                    # thinking/signature blocks) for conversation continuation.
                    "assistant_message": assistant_message,
                }
            },
            http_status=status,
            provider_request_id=self._request_id(response_headers),
            observed=observed_response_facts(
                response_headers,
                body_model=(str(data.get("model")) if data.get("model") else None),
                protocol="claude_messages",
                channel_id=self.channel_id,
            ),
        )

    async def _user_content(self, context: V2ExecutionContext) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for material_id in context.material_ids:
            resolved = await self.materials.resolve(
                material_id,
                tenant_id=context.tenant_id,
                conversation_hash=context.conversation_hash,
                with_bytes=True,
            )
            data = resolved.data or b""
            mime = str(resolved.material.get("content_type") or "application/octet-stream").lower()
            filename = str(resolved.material.get("filename") or material_id)
            if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
                text = data.decode("utf-8", errors="strict")
                parts.append({"type": "text", "text": f"[SOURCE_FILE:{filename}]\n{text}\n[END_SOURCE_FILE:{filename}]"})
            elif mime.startswith("image/"):
                parts.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }
                )
            elif mime == "application/pdf":
                parts.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }
                )
            else:
                raise ProviderRequestError(
                    "MATERIAL_REPRESENTATION_UNSUPPORTED",
                    f"Claude Messages has no verified Relay projection for {mime!r} ({filename})",
                )
        value = context.snapshot.get("input")
        query = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts.append({"type": "text", "text": query})
        return parts

    @staticmethod
    def _apply_thinking_and_output(payload: dict[str, Any], snapshot: dict[str, Any]) -> None:
        contract = snapshot.get("capability_contract")
        thinking = contract.get("thinking") if isinstance(contract, dict) and isinstance(contract.get("thinking"), dict) else {}
        strategy = str(thinking.get("wire_strategy") or "").lower()
        level = str(snapshot.get("think_level") or "auto").lower()
        output_config: dict[str, Any] = {}
        if strategy == "claude_adaptive_effort":
            if level == "off":
                payload["thinking"] = {"type": "disabled"}
            else:
                payload["thinking"] = {"type": "adaptive"}
                if level not in {"", "auto"}:
                    output_config["effort"] = level

        try:
            spec = resolve_structured_output(
                snapshot,
                fallback_name=str((snapshot.get("metadata") or {}).get("stage") or "structured_output"),
            )
        except StructuredOutputError as exc:
            raise ProviderRequestError(exc.code, exc.message) from exc
        if spec is not None:
            guarantee = str(snapshot.get("structured_output_guarantee") or "legacy").lower()
            if guarantee != "native_json_schema":
                raise ProviderRequestError(
                    "STRUCTURED_OUTPUT_CONTRACT_MISMATCH",
                    "Claude Messages structured output requires a native_json_schema capability contract",
                )
            if spec.mode == "json_object":
                # Canonical json_object has no schema to constrain natively; use
                # a minimal JSON value schema and keep Relay post-validation.
                schema: dict[str, Any] = {"type": "object"}
            else:
                schema = dict(spec.schema or {})
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            payload["output_config"] = output_config

    @staticmethod
    def _request_id(headers: dict[str, Any]) -> str | None:
        for key in ("request-id", "x-request-id", "anthropic-request-id", "x-trace-id"):
            value = headers.get(key)
            if value:
                return str(value)
        return None
