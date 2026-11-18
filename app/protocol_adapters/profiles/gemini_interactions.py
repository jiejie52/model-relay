"""Gemini Interactions Protocol Adapter.

Implements official Google Gemini Interactions API (/v1beta/interactions) with
native context continuation, previous_interaction_id support, and incremental delta input slicing.
"""
from __future__ import annotations

import copy
import json
from typing import Any

import httpx

from app.config import Settings
from app.protocol_adapters.base import ProviderHTTPError, ProviderRequestError
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
                request_id=self._request_id(response_headers),
                response_headers=response_headers,
                phase="gemini_interactions_parse",
            )

        # Extract interaction ID (new anchor handle)
        response_id = str(data.get("id") or data.get("interaction_id") or "") or None

        # Extract steps & text
        steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        model_texts: list[str] = []
        thought_steps: list[dict[str, Any]] = []

        for step in steps:
            if isinstance(step, dict):
                stype = step.get("type")
                if stype == "model_output":
                    t = step.get("text") or (step.get("content") or {}).get("text")
                    if t:
                        model_texts.append(str(t))
                elif stype == "thought":
                    thought_steps.append(step)
                elif stype == "text" and "text" in step:
                    model_texts.append(str(step["text"]))

        text = "\n".join(model_texts)
        if not text and isinstance(data.get("output"), str):
            text = data["output"]
        elif not text and isinstance(data.get("text"), str):
            text = data["text"]

        # Usage metadata
        usage_meta = data.get("usage") or data.get("usageMetadata") or data.get("usage_metadata") or {}
        if not isinstance(usage_meta, dict):
            usage_meta = {}

        prompt_tokens = usage_meta.get("prompt_tokens") or usage_meta.get("promptTokenCount")
        completion_tokens = (
            usage_meta.get("candidates_tokens")
            or usage_meta.get("candidatesTokenCount")
            or usage_meta.get("completion_tokens")
        )
        total_tokens = usage_meta.get("total_tokens") or usage_meta.get("totalTokenCount")
        cached_tokens = (
            usage_meta.get("cached_tokens")
            or usage_meta.get("cached_content_token_count")
            or usage_meta.get("cachedContentTokenCount")
        )

        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
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
            "actual_model": data.get("model") or context.snapshot.get("model"),
        }

        return StandardizedProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=response_id,
            usage=usage,
            cached_tokens=cached_tokens,
            response_output={"steps": steps, "text": text},
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
