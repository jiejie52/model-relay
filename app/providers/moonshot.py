from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

import httpx

from ..config import Settings
from ..structured_output import (
    StructuredOutputError,
    apply_openai_chat_structured_output,
    validate_against_schema,
)
from .base import (
    ProviderHTTPError,
    ProviderRequestError,
    ProviderResult,
    ProviderTransportError,
)


class MoonshotChatCompletionsProvider:
    """Official Moonshot/Kimi Chat Completions adapter.

    The adapter preserves the complete assistant message in session history so
    provider-native fields such as reasoning_content and tool_calls survive
    multi-turn replay. Relay Core treats that history record as opaque data.
    """

    provider_id = "moonshot"
    protocol = "openai-chat-completions"
    history_codec = "moonshot-chat/1"

    _PROTECTED_OVERRIDE_KEYS = {
        "model",
        "messages",
        "stream",
        "response_format",
        "prompt_cache_key",
        "reasoning_effort",
        "thinking",
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def capability_profile_version(model: str) -> str:
        value = str(model or "").strip().lower()
        if value.startswith("kimi-k3"):
            return "moonshot-k3-chat/2026-09-17"
        if value.startswith("kimi-k2.6"):
            return "moonshot-k2.6-chat/2026-09-17"
        return "moonshot-chat-generic/1"

    async def execute(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any] | None,
        material_prefix: Any,
        history: list[dict[str, Any]],
    ) -> ProviderResult:
        if self.settings.moonshot_api_key is None:
            raise ProviderRequestError(
                "MOONSHOT_CREDENTIAL_MISSING",
                "MOONSHOT_API_KEY is not configured for the Moonshot provider profile",
            )

        model = str(request_snapshot.get("model") or "").strip()
        if not model:
            raise ProviderRequestError("MODEL_REQUIRED", "Kimi job requires model")

        messages: list[dict[str, Any]] = []
        instructions = str(request_snapshot.get("instructions") or "").strip()
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.extend(self._context_messages(material_prefix))

        for turn in history:
            if not isinstance(turn, dict):
                continue
            codec = str(turn.get("codec") or "")
            if codec and codec != self.history_codec:
                raise ProviderRequestError(
                    "SESSION_HISTORY_CODEC_MISMATCH",
                    f"Moonshot session cannot replay history codec {codec!r}",
                )
            native_messages = turn.get("messages")
            if isinstance(native_messages, list):
                messages.extend(
                    deepcopy([m for m in native_messages if isinstance(m, dict)])
                )
                continue
            # Defensive compatibility with early Moonshot history shapes.
            user_message = turn.get("user_message")
            assistant_message = turn.get("assistant_message")
            if isinstance(user_message, dict):
                messages.append(deepcopy(user_message))
            if isinstance(assistant_message, dict):
                messages.append(deepcopy(assistant_message))

        current_messages = self._current_messages(request_snapshot)
        messages.extend(deepcopy(current_messages))
        if not messages:
            raise ProviderRequestError("INPUT_REQUIRED", "Kimi job has no messages")

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        if session and session.get("prompt_cache_key"):
            payload["prompt_cache_key"] = session["prompt_cache_key"]

        generation = request_snapshot.get("generation")
        if isinstance(generation, dict):
            for key, value in generation.items():
                if key not in self._PROTECTED_OVERRIDE_KEYS and key not in {"reasoning", "thinking"}:
                    payload[key] = value
            self._apply_reasoning(payload, model, generation, request_snapshot)
        else:
            self._apply_reasoning(payload, model, {}, request_snapshot)

        provider_payload = request_snapshot.get("provider_payload") or {}
        if isinstance(provider_payload, dict):
            for key, value in provider_payload.items():
                if key not in self._PROTECTED_OVERRIDE_KEYS:
                    payload[key] = value

        try:
            structured_spec = apply_openai_chat_structured_output(
                payload,
                request_snapshot,
                fallback_name=str(request_snapshot.get("label") or request_snapshot.get("stage") or "structured_output"),
                provider="moonshot",
                model=model,
            )
        except StructuredOutputError as exc:
            raise ProviderRequestError(exc.code, exc.message) from exc

        url = f"{self.settings.moonshot_root}/chat/completions"
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=None,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        headers = {
            "Authorization": f"Bearer {self.settings.moonshot_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
                response = await client.post(url, headers=headers, json=payload)
                raw = await response.aread()
        except httpx.HTTPError as exc:
            raise ProviderTransportError(
                str(exc),
                provider="moonshot",
                service="moonshot-api",
                exception_type=type(exc).__name__,
                cause_chain=self._cause_chain(exc),
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise ProviderHTTPError(
                response.status_code,
                raw,
                headers=self._headers_raw(response),
                content_type=response.headers.get("content-type"),
                provider="moonshot",
                service="moonshot-api",
                request_id=self._request_id(response),
            )

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ProviderHTTPError(
                response.status_code,
                raw,
                headers=self._headers_raw(response),
                content_type=response.headers.get("content-type"),
                provider="moonshot",
                service="moonshot-api",
                request_id=self._request_id(response),
                message="Moonshot success response was not valid JSON",
            ) from exc

        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else {}
        assistant_message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(assistant_message, dict):
            raise ProviderRequestError(
                "MOONSHOT_RESPONSE_MESSAGE_MISSING",
                "Moonshot response did not contain choices[0].message",
            )
        assistant_message = deepcopy(assistant_message)
        text = self._message_text(assistant_message)

        if structured_spec is not None and text:
            try:
                value = json.loads(text)
                validate_against_schema(value, structured_spec)
            except (json.JSONDecodeError, StructuredOutputError) as exc:
                if isinstance(exc, StructuredOutputError):
                    raise ProviderRequestError(exc.code, exc.message) from exc
                raise ProviderRequestError(
                    "STRUCTURED_OUTPUT_INVALID_JSON",
                    f"Kimi structured output was not valid JSON: {exc}",
                ) from exc

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cached_tokens = usage.get("cached_tokens") if isinstance(usage.get("cached_tokens"), int) else None
        finish_reason = first.get("finish_reason") if isinstance(first, dict) else None

        return ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=str(data.get("id") or "") or None,
            usage=usage,
            cached_tokens=cached_tokens,
            # response_output is kept for the generic artifact path; the complete
            # assistant message is also retained in history_record.
            response_output=[assistant_message],
            history_record={
                "codec": self.history_codec,
                "messages": deepcopy(current_messages) + [assistant_message],
                "response_id": str(data.get("id") or "") or None,
            },
            finish_reason=str(finish_reason) if finish_reason is not None else None,
        )

    @classmethod
    def _apply_reasoning(
        cls,
        payload: dict[str, Any],
        model: str,
        generation: dict[str, Any],
        request_snapshot: dict[str, Any],
    ) -> None:
        reasoning = generation.get("reasoning")
        thinking = generation.get("thinking")
        legacy = str(request_snapshot.get("think_level") or "auto").lower()

        if model.lower().startswith("kimi-k3"):
            effort = None
            if isinstance(reasoning, dict):
                effort = reasoning.get("effort")
            if effort is None and legacy in {"low", "high"}:
                effort = legacy
            if effort is not None:
                effort = str(effort).lower()
                if effort not in {"low", "high", "max"}:
                    raise ProviderRequestError(
                        "MOONSHOT_REASONING_EFFORT_UNSUPPORTED",
                        "Kimi K3 reasoning.effort must be one of low, high, max",
                    )
                payload["reasoning_effort"] = effort
            elif legacy not in {"", "auto"}:
                raise ProviderRequestError(
                    "MOONSHOT_LEGACY_THINK_LEVEL_UNMAPPABLE",
                    f"Legacy think_level={legacy!r} is not mapped silently for Kimi K3; use generation.reasoning.effort",
                )
            if thinking is not None:
                raise ProviderRequestError(
                    "MOONSHOT_THINKING_PARAMETER_UNSUPPORTED",
                    "Kimi K3 uses reasoning_effort rather than generation.thinking",
                )
            return

        if isinstance(thinking, dict):
            payload["thinking"] = deepcopy(thinking)
        elif legacy not in {"", "auto"}:
            raise ProviderRequestError(
                "MOONSHOT_LEGACY_THINK_LEVEL_UNMAPPABLE",
                f"Legacy think_level={legacy!r} is not mapped silently for this Kimi model; use generation.thinking",
            )
        if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
            raise ProviderRequestError(
                "MOONSHOT_REASONING_EFFORT_UNSUPPORTED",
                "generation.reasoning.effort is reserved for Kimi K3 in this adapter",
            )

    @classmethod
    def _context_messages(cls, context: Any) -> list[dict[str, Any]]:
        if context is None:
            return []
        if isinstance(context, dict) and isinstance(context.get("messages"), list):
            return [cls._normalize_message(x) for x in context["messages"] if isinstance(x, dict)]
        if isinstance(context, list):
            return [cls._normalize_message(x) for x in context if isinstance(x, dict)]
        if isinstance(context, dict) and "role" in context:
            return [cls._normalize_message(context)]
        if isinstance(context, str):
            return [{"role": "user", "content": context}]
        return [
            {
                "role": "user",
                "content": "[Relay immutable context JSON]\n" + json.dumps(context, ensure_ascii=False, default=str),
            }
        ]

    @classmethod
    def _current_messages(cls, request_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        value = request_snapshot.get("input")
        if isinstance(value, list):
            return [cls._normalize_message(x) for x in value if isinstance(x, dict)]
        query = request_snapshot.get("current_query")
        if query is None:
            return []
        return [{"role": "user", "content": str(query)}]

    @staticmethod
    def _normalize_message(message: dict[str, Any]) -> dict[str, Any]:
        out = deepcopy(message)
        content = out.get("content")
        if isinstance(content, list):
            converted = []
            for part in content:
                if not isinstance(part, dict):
                    converted.append(part)
                    continue
                part_type = str(part.get("type") or "")
                if part_type == "input_text":
                    converted.append({"type": "text", "text": str(part.get("text") or "")})
                    continue
                if part_type in {"image_url", "video_url"}:
                    value = part.get(part_type)
                    if isinstance(value, dict):
                        url = str(value.get("url") or "")
                    else:
                        url = str(value or "")
                    if url and not (url.startswith("data:") or url.startswith("ms://")):
                        raise ProviderRequestError(
                            "MOONSHOT_REMOTE_MEDIA_URL_UNSUPPORTED",
                            "Kimi media input must use data: base64 or ms:// file references; ordinary remote URLs are not forwarded silently",
                        )
                    converted.append(part)
                    continue
                if part_type in {"input_image", "input_file"}:
                    raise ProviderRequestError(
                        "MOONSHOT_MATERIAL_PREPARATION_REQUIRED",
                        "Legacy input_image/input_file transport must be prepared as Kimi text, data: media, or ms:// references before provider execution",
                    )
                converted.append(part)
            out["content"] = converted
        return out

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}:
                    text = part.get("text")
                    if isinstance(text, str):
                        pieces.append(text)
            return "\n".join(pieces)
        return ""

    @staticmethod
    def _headers_raw(response: httpx.Response) -> list[tuple[str, str]]:
        return [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in response.headers.raw
        ]

    @staticmethod
    def _request_id(response: httpx.Response) -> str | None:
        for name in (
            "x-request-id",
            "request-id",
            "msh-request-id",
            "msh-request-signature",
        ):
            value = response.headers.get(name)
            if value:
                return value
        return None

    @staticmethod
    def _cause_chain(exc: BaseException) -> list[str]:
        out: list[str] = []
        current: BaseException | None = exc
        while current is not None and len(out) < 8:
            out.append(f"{type(current).__name__}: {current}")
            current = current.__cause__ or current.__context__
        return out
