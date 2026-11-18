"""Gemini Interactions Protocol Adapter.

Implements official Google Gemini Interactions API (/v1beta/interactions) with
native context continuation, previous_interaction_id support, and incremental delta input slicing.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import Settings
from app.observability.response_capture import safe_response_headers
from app.protocol_adapters.base import ProviderHTTPError
from app.protocol_adapters.native_evidence import native_response_facts
from app.protocol_adapters.v2_base import SealedDispatchBundle, StandardizedProviderResult
from app.protocol_adapters.wire_projection.gemini_interactions_wire import project_gemini_interactions_payload
from app.protocol_adapters.wire_projection.http_wire import decode_entity, observed_response_facts, read_raw_response
from app.runtime.transport_guard import provider_transport


class GeminiInteractionsAdapter:
    """Gemini Interactions Adapter over Google official Interactions API."""

    adapter_version = "gemini-interactions/1"

    def __init__(self, settings: Settings, *, connection=None, transport=None):
        if connection is None:
            from app.compatibility.connection_config import legacy_runtime
            connection = legacy_runtime(settings, "gemini_interactions")
        self.connection = connection
        self.settings = settings
        self.base_url = connection.rest_base
        self._transport = transport

    async def execute(self, context: SealedDispatchBundle) -> StandardizedProviderResult:
        """Execute request against POST /v1beta/interactions with integrity verification."""
        context.verify_integrity()

        payload, user_content = project_gemini_interactions_payload(context, connection=self.connection)

        url = self.connection.api_url("interactions")
        headers = {
            **self.connection.headers(),
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

        async with httpx.AsyncClient(
            timeout=timeout,
            verify=True,
            transport=provider_transport(
                self.settings,
                "gemini",
                self._transport,
                url,
                connection=self.connection,
            ),
        ) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                raw = await read_raw_response(
                    response,
                    log_context={
                        "request_id": context.request_id,
                        "session_id": context.session_id,
                        "provider": "gemini",
                        "connection_id": context.snapshot.get("connection_id"),
                        "model": context.snapshot.get("model"),
                        "protocol": "gemini_interactions",
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
                response_headers=response_headers,
                phase="gemini_interactions",
            )

        try:
            decoded = decode_entity(raw, response_headers.get("content-encoding"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            raise ProviderHTTPError(
                status,
                raw,
                "Gemini interactions success response was not valid JSON",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
                response_headers=response_headers,
                phase="gemini_interactions_parse",
            ) from exc

        if not isinstance(data, dict):
            raise ProviderHTTPError(
                status,
                raw,
                "Gemini interactions response must be a JSON object",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers),
                response_headers=response_headers,
                phase="gemini_interactions_parse",
            )

        # A 2xx transport response does not imply a completed generation.
        # This synchronous adapter never polls, continues tools, or re-sends.
        interaction_status = data.get("status")
        if (interaction_status is not None and interaction_status != "completed") or data.get("error"):
            exc = ProviderHTTPError(
                status, raw, f"Gemini interaction did not complete (status={interaction_status!r})",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=self._request_id(response_headers), response_headers=response_headers,
                phase="gemini_interactions_status",
            )
            exc.interaction_status = interaction_status
            exc.unknown_completion = interaction_status not in (None, "completed", "failed", "cancelled", "incomplete")
            raise exc

        # store=false responses can legitimately omit the interaction ID.
        response_id = str(data.get("id") or data.get("interaction_id") or "") or None
        steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        model_texts: list[str] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("type") == "model_output":
                content = step.get("content")
                if isinstance(content, (list, dict, str)):
                    model_texts.extend(_content_texts(content))
                elif isinstance(step.get("text"), str):
                    model_texts.append(step["text"])
            elif step.get("type") == "text" and isinstance(step.get("text"), str):
                model_texts.append(step["text"])
        # Retain legacy response compatibility without exposing thought/signature,
        # tool arguments, or multimodal payloads as assistant text.
        if not model_texts:
            model_texts.extend(_content_texts(data.get("outputs")))
        text = "".join(model_texts)
        if not model_texts:
            for key in ("output", "text"):
                if isinstance(data.get(key), str):
                    text = data[key]
                    break

        usage_meta = next((data[k] for k in ("usage", "usageMetadata", "usage_metadata")
                           if isinstance(data.get(k), dict)), {})
        prompt_tokens = _token_count(usage_meta, "total_input_tokens", "prompt_tokens", "promptTokenCount")
        completion_tokens = _token_count(usage_meta, "total_output_tokens", "candidates_tokens",
                                         "candidatesTokenCount", "completion_tokens")
        total_tokens = _token_count(usage_meta, "total_tokens", "totalTokenCount")
        cached_tokens = _token_count(usage_meta, "total_cached_tokens", "cached_tokens",
                                     "cached_content_token_count", "cachedContentTokenCount")
        usage = {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": total_tokens, "cached_tokens": cached_tokens,
            "thinking_tokens": _token_count(usage_meta, "total_thought_tokens", "thoughtsTokenCount"),
            "tool_use_tokens": _token_count(usage_meta, "total_tool_use_tokens"),
            "raw": usage_meta,
        }

        prev_interaction_id = payload.get("previous_interaction_id")
        transport_history: dict[str, Any] = {
            "kind": "gemini_interactions",
            "interaction_id": response_id,
            "previous_interaction_id": prev_interaction_id,
            "interaction_user_input": user_content,
            "interaction_model_steps": steps,
            "user_content": user_content,
            "model_content": {"role": "model", "parts": [{"text": text}]},
            "steps": steps,
        }

        observed = {
            "protocol": "gemini_interactions",
            "interaction_id": response_id,
            "upstream_handle": response_id,
            "continuation_handle": response_id,
            "previous_interaction_id": prev_interaction_id,
            "cached_tokens": cached_tokens,
            "interaction_status": interaction_status,
            "response_model": data.get("model"),
            "actual_model": data.get("model"),
            "response_headers": safe_response_headers(response_headers),
        }

        observed.update(observed_response_facts(
            response_headers, body_model=data.get("model"),
            channel_id=context.snapshot.get("channel_id"), protocol="gemini_interactions",
            evidence_headers=(((context.snapshot.get("protocol_profile") or {}).get("metadata") or {}).get("response_evidence_headers")),
        ))
        observed = native_response_facts(data, payload, context, self.connection, observed)
        return StandardizedProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=response_id,
            usage=usage,
            cached_tokens=cached_tokens,
            response_output={"steps": steps, "text": text, "status": interaction_status},
            history_entry={"transport_history": transport_history},
            http_status=status,
            provider_request_id=self._request_id(response_headers),
            observed=observed,
        )

    def _request_id(self, headers: dict[str, str]) -> str | None:
        for k in ("x-goog-request-id", "x-request-id", "request-id"):
            val = headers.get(k) or headers.get(k.lower())
            if val:
                return str(val)
        return None


def _content_texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [text for item in content for text in _content_texts(item)]
    if isinstance(content, dict) and content.get("type") in {None, "text"} and isinstance(content.get("text"), str):
        return [content["text"]]
    return []


def _token_count(usage, *names):
    return next((usage[name] for name in names if type(usage.get(name)) is int and usage[name] >= 0), None)
