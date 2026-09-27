from __future__ import annotations

import json
from typing import Any

import httpx

from .base import ProviderHTTPError, ProviderRequestError
from .http_wire import decode_entity, observed_response_facts, read_raw_response
from .v2_base import V2ExecutionContext, V2ProviderResult
from ..config import Settings
from ..materials.resolver import MaterialResolver
from ..structured_output import StructuredOutputError, resolve_structured_output


class ChatCompletionsV2Adapter:
    """Reusable OpenAI-compatible Chat Completions protocol adapter.

    Model-specific execution semantics are taken from the frozen capability
    contract. The adapter owns only the native wire projection; it never accepts
    arbitrary caller provider JSON.
    """

    adapter_version = "chat-completions-v2/1"

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
        instructions = context.snapshot.get("instructions")
        if instructions:
            messages.append({"role": "system", "content": str(instructions)})

        if context.session.get("context_policy") == "conversation":
            for turn in context.history:
                transport = turn.get("transport_history") if isinstance(turn, dict) else None
                if not isinstance(transport, dict) or transport.get("kind") != "chat_completions":
                    continue
                user_message = transport.get("user_message")
                assistant_message = transport.get("assistant_message")
                if isinstance(user_message, dict):
                    messages.append(user_message)
                if isinstance(assistant_message, dict):
                    messages.append(assistant_message)

        material_text = await self._material_text(context)
        input_value = context.snapshot.get("input")
        query = input_value if isinstance(input_value, str) else json.dumps(
            input_value, ensure_ascii=False, separators=(",", ":")
        )
        content = f"{material_text}\n\n{query}" if material_text else query
        user_message = {"role": "user", "content": content}
        messages.append(user_message)

        payload: dict[str, Any] = {
            "model": str(context.snapshot["model"]),
            "messages": messages,
            "stream": False,
        }
        self._apply_options(payload, context.snapshot)
        self._apply_thinking(payload, context.snapshot)
        self._apply_structured_output(payload, context.snapshot)

        url = f"{self.base_url}/chat/completions"
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=None,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
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
                "Chat Completions success response was not valid JSON",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
            ) from exc

        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise ProviderHTTPError(
                status,
                raw,
                "Chat Completions response has no assistant message",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
            )

        assistant_message = dict(message)
        assistant_message.setdefault("role", "assistant")
        text = self._message_text(assistant_message)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cached_tokens = self._cached_tokens(usage)
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
                    "kind": "chat_completions",
                    "user_message": user_message,
                    # Keep provider-derived reasoning_content and any other
                    # transport fields required for a future continuation.
                    "assistant_message": assistant_message,
                }
            },
            http_status=status,
            provider_request_id=self._request_id(response_headers),
            observed=observed_response_facts(
                response_headers,
                body_model=(str(data.get("model")) if data.get("model") else None),
                protocol="chat_completions",
                channel_id=self.channel_id,
            ),
        )

    async def _material_text(self, context: V2ExecutionContext) -> str:
        blocks: list[str] = []
        for material_id in context.material_ids:
            resolved = await self.materials.resolve(
                material_id,
                tenant_id=context.tenant_id,
                conversation_hash=context.conversation_hash,
                with_bytes=True,
            )
            mime = str(resolved.material.get("content_type") or "application/octet-stream").lower()
            filename = str(resolved.material.get("filename") or material_id)
            if not (
                mime.startswith("text/")
                or mime in {"application/json", "application/xml", "application/yaml", "application/x-yaml"}
            ):
                raise ProviderRequestError(
                    "MATERIAL_REPRESENTATION_UNSUPPORTED",
                    f"Chat Completions connection only has a verified text-material contract; unsupported {mime!r} for {filename}",
                )
            data = resolved.data or b""
            text = data.decode("utf-8", errors="strict")
            blocks.append(f"[SOURCE_FILE:{filename}]\n{text}\n[END_SOURCE_FILE:{filename}]")
        return "\n\n".join(blocks)

    @staticmethod
    def _apply_options(payload: dict[str, Any], snapshot: dict[str, Any]) -> None:
        options = snapshot.get("effective_options") or {}
        if not isinstance(options, dict):
            return
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if "top_p" in options:
            payload["top_p"] = options["top_p"]
        if "max_output_tokens" in options:
            payload["max_tokens"] = options["max_output_tokens"]

    @staticmethod
    def _apply_thinking(payload: dict[str, Any], snapshot: dict[str, Any]) -> None:
        contract = snapshot.get("capability_contract")
        thinking = contract.get("thinking") if isinstance(contract, dict) and isinstance(contract.get("thinking"), dict) else {}
        strategy = str(thinking.get("wire_strategy") or "").lower()
        level = str(snapshot.get("think_level") or "auto").lower()
        if strategy == "chat_reasoning_effort_with_thinking_on":
            payload["thinking"] = {"type": "enabled"}
            if level not in {"", "auto", "on"}:
                payload["reasoning_effort"] = level
        elif strategy == "chat_thinking_toggle":
            if level == "on":
                payload["thinking"] = {"type": "enabled"}
            elif level == "off":
                payload["thinking"] = {"type": "disabled"}
        elif strategy == "chat_reasoning_effort" and level not in {"", "auto"}:
            payload["reasoning_effort"] = level

    @staticmethod
    def _apply_structured_output(payload: dict[str, Any], snapshot: dict[str, Any]) -> None:
        try:
            spec = resolve_structured_output(
                snapshot,
                fallback_name=str((snapshot.get("metadata") or {}).get("stage") or "structured_output"),
            )
        except StructuredOutputError as exc:
            raise ProviderRequestError(exc.code, exc.message) from exc
        if spec is None:
            return
        mode = str(snapshot.get("structured_output_guarantee") or "legacy").lower()
        if mode == "native_json_schema" and spec.mode == "json_schema" and spec.schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": spec.name or "structured_output",
                    "strict": spec.strict,
                    "schema": spec.schema,
                },
            }
            return
        # For contracts that only verify JSON-object transport, the full
        # canonical JSON Schema is intentionally enforced after the provider
        # response by SharedExecutionRuntime.
        payload["response_format"] = {"type": "json_object"}

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text"))
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        return ""

    @staticmethod
    def _cached_tokens(usage: dict[str, Any]) -> int | None:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
            return int(details["cached_tokens"])
        return None

    @staticmethod
    def _request_id(headers: dict[str, Any]) -> str | None:
        for key in ("x-request-id", "request-id", "x-trace-id", "trace-id"):
            value = headers.get(key)
            if value:
                return str(value)
        return None
