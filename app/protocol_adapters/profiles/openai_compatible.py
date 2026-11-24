import json
from typing import Any

import httpx

from app.observability.http_wire_logger import affinity_hint, normalization_active
from app.runtime.transport_guard import provider_transport
from app.config import Settings
from app.core.idempotency import CANONICAL_REQUEST_VERSIONS, V3_FENCED_REQUEST_VERSIONS
from app.protocol_adapters.base import ProviderHTTPError, ProviderRequestError, ProviderResult
from app.protocol_adapters.wire_projection.http_wire import read_raw_response, decode_entity
from app.canonical_schema import StructuredOutputError
from app.protocol_adapters.wire_projection.schema_projector import apply_openai_responses_structured_output


class OpenAICompatibleResponsesProvider:
    """OpenAI-compatible /responses adapter.

    The Relay infrastructure is model-agnostic. Current special handling is kept
    inside this provider adapter, including Grok store=false, encrypted reasoning
    replay, prompt_cache_key and reasoning.effort mapping.
    """

    _PROTECTED_OVERRIDE_KEYS = {
        "model",
        "input",
        "store",
        "include",
        "instructions",
        "prompt_cache_key",
        "previous_interaction_id",
        "previous_response_id",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        connection_id: str = "aihubmix_default",
        channel_id: str = "aihubmix",
    ) -> None:
        self.settings = settings
        self.base_url = (base_url or settings.aihubmix_root).rstrip("/")
        self.api_key = api_key
        self.connection_id = connection_id
        self.channel_id = channel_id

    def project_payload(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any] | None,
        material_prefix: Any,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        provider = str(request_snapshot.get("provider", "")).lower()
        model = str(request_snapshot["model"])
        think_level = str(request_snapshot.get("think_level") or "auto").lower()

        input_items: list[Any] = []
        input_items.extend(self._material_items(material_prefix))

        for turn in history:
            user_item = turn.get("user_item")
            if user_item:
                input_items.append(user_item)
            previous_output = turn.get("response_output") or []
            if isinstance(previous_output, list):
                input_items.extend(previous_output)

        override_user_item = request_snapshot.get("_relay_responses_current_user_item")
        current_user_item = (
            override_user_item
            if isinstance(override_user_item, dict)
            else self.make_user_item(str(request_snapshot["current_query"]))
        )
        prefix_has_query = bool(request_snapshot.get("material_prefix_includes_current_query"))
        if not (request_snapshot.get("mode") == "new_session" and prefix_has_query):
            input_items.append(current_user_item)

        store_upstream, parent_id = self._resolve_continuation_control(request_snapshot)
        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "store": store_upstream,
        }

        instructions = request_snapshot.get("instructions")
        if instructions and request_snapshot.get("normalized_head") is None:
            payload["instructions"] = instructions
        tools = request_snapshot.get("tools")
        if request_snapshot.get("normalized_head") is None and isinstance(tools, list) and tools:
            if str(request_snapshot.get("schema_version") or "") in V3_FENCED_REQUEST_VERSIONS:
                from app.core.context_codec import normalize_native_payload
                unordered = (((request_snapshot.get("protocol_profile") or {}).get("cache") or {}).get("unordered_tools") is True)
                payload["tools"] = normalize_native_payload(
                    {"tools": json.loads(json.dumps(tools))},
                    response_schemas=True,
                    unordered_tools=unordered,
                )["tools"]
            else:
                payload["tools"] = json.loads(json.dumps(tools))

        self._apply_reasoning(
            payload,
            request_snapshot,
            provider=provider,
            model=model,
            think_level=think_level,
            session=session,
        )
        self._apply_cache_projection(
            payload,
            request_snapshot,
            provider=provider,
            model=model,
        )
        from app.protocol_adapters.wire_projection.responses_wire import project_responses_head
        project_responses_head(payload, request_snapshot, parent_id)
        if parent_id:
            payload["previous_response_id"] = parent_id
        if store_upstream and request_snapshot.get("normalized_head") is not None:
            # Stateful rehydration uses the provider's stored reasoning. In
            # particular xAI's explicit encrypted include can disable that.
            payload.pop("include", None)
        if request_snapshot.get("normalized_head") is not None:
            retention = request_snapshot.get("upstream_retention") or {}
            if (store_upstream or parent_id) and retention.get("allow_store") is not True:
                raise ProviderRequestError("CONTINUATION_STORAGE_NOT_AUTHORIZED", "Upstream response storage is not authorized")

        self._apply_model_options(payload, request_snapshot)

        # Generic structured-output passthrough. The adapter never inspects the
        # schema's business property names; it only maps the caller's JSON Schema
        # to the OpenAI-compatible Responses transport. This deliberately runs
        # after canonical option projection so an explicit caller schema remains
        # authoritative over any legacy/fallback json_object setting.
        try:
            apply_openai_responses_structured_output(
                payload,
                request_snapshot,
                fallback_name="structured_output" if normalization_active() else str(request_snapshot.get("stage") or "structured_output"),
            )
        except StructuredOutputError as exc:
            raise ProviderRequestError(exc.code, exc.message) from exc

        return payload

    async def execute(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any] | None,
        material_prefix: Any,
        history: list[dict[str, Any]],
    ) -> ProviderResult:
        payload = self.project_payload(request_snapshot, session=session,
                                       material_prefix=material_prefix, history=history)
        base_url = (
            ((request_snapshot.get("upstream") or {}).get("base_url"))
            or self.base_url
        ).rstrip("/")
        url = f"{base_url}/responses"

        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=None,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        api_key = self.api_key or (
            self.settings.aihubmix_api_key.get_secret_value()
            if self.settings.aihubmix_api_key is not None
            else None
        )
        if not api_key:
            raise ProviderRequestError(
                "CONNECTION_NOT_CONFIGURED",
                f"Credential is not configured for connection {self.connection_id}",
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

        async with httpx.AsyncClient(timeout=timeout, verify=True, transport=provider_transport(self.settings, "responses", url=url)) as client:
            if hasattr(client, "stream"):
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    raw = await read_raw_response(response, log_context=request_snapshot.get("_relay_log_context"))
                    response_status = response.status_code
                    response_headers = dict(getattr(response, "headers", {}) or {})
            else:  # test doubles / older compatible clients
                response = await client.post(url, headers=headers, json=payload)
                raw = await response.aread()
                response_status = response.status_code
                response_headers = dict(getattr(response, "headers", {}) or {})

        if response_status < 200 or response_status >= 300:
            raise ProviderHTTPError(
                response_status,
                raw,
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=(response_headers.get("x-request-id") or response_headers.get("request-id")),
            )

        try:
            decoded = decode_entity(raw, response_headers.get("content-encoding"))
            data = json.loads(decoded.decode("utf-8"))
        except (ValueError, OSError) as cause:
            exc = ProviderHTTPError(
                response_status, raw, "Upstream response was not valid JSON",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=response_headers.get("x-request-id") or response_headers.get("request-id"),
                response_headers=response_headers, phase="responses_completion",
            )
            exc.code = "RESPONSES_GENERATION_UNKNOWN"
            exc.unknown_completion = True
            raise exc from cause

        from app.protocol_adapters.wire_projection.responses_wire import observe_responses_outcome

        outcome = observe_responses_outcome(data)
        legacy_missing_status = (
            isinstance(data, dict) and ("status" not in data or data["status"] is None)
            and not data.get("error") and not payload["store"]
            and not request_snapshot.get("response_completion_contract")
        )
        if outcome["completion"] != "completed" and not legacy_missing_status:
            exc = ProviderHTTPError(response_status, raw, "Responses generation did not complete",
                content_type=response_headers.get("content-type"),
                content_encoding=response_headers.get("content-encoding"),
                request_id=response_headers.get("x-request-id"), response_headers=response_headers,
                phase="responses_completion")
            exc.code = "RESPONSES_GENERATION_" + outcome["completion"].upper()
            exc.unknown_completion = outcome["completion"] == "unknown"
            raise exc

        text = self.extract_visible_text(data)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cached_tokens = self.extract_cached_tokens(usage)
        output = data.get("output") if isinstance(data.get("output"), list) else []

        return ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=data.get("id"),
            usage=usage,
            cached_tokens=cached_tokens,
            response_output=output,
            http_status=response_status,
            provider_request_id=(response_headers.get("x-request-id") or response_headers.get("request-id")),
            response_headers=response_headers,
        )


    @classmethod
    def _apply_reasoning(
        cls,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        provider: str,
        model: str,
        think_level: str,
        session: dict[str, Any] | None,
    ) -> None:
        contract = snapshot.get("capability_contract")
        thinking = contract.get("thinking") if isinstance(contract, dict) and isinstance(contract.get("thinking"), dict) else {}
        strategy = str(thinking.get("wire_strategy") or "").lower()
        from app.protocol_adapters.thinking_projection import project_thinking
        if project_thinking(payload, snapshot, "responses"):
            if thinking.get("include_encrypted_reasoning"):
                payload["include"] = ["reasoning.encrypted_content"]
                if session and session.get("prompt_cache_key"): payload["prompt_cache_key"] = session["prompt_cache_key"]
            return
        if strategy == "responses_reasoning_effort":
            if bool(thinking.get("include_encrypted_reasoning")):
                payload["include"] = ["reasoning.encrypted_content"]
                if session and session.get("prompt_cache_key"):
                    payload["prompt_cache_key"] = session["prompt_cache_key"]
            if think_level not in {"", "auto", "on", "off"}:
                payload["reasoning"] = {"effort": think_level}
            elif think_level == "none":
                payload["reasoning"] = {"effort": "none"}
            return

        # Compatibility for persisted pre-2.0 Grok Requests.
        if cls._is_grok(provider, model):
            payload["include"] = ["reasoning.encrypted_content"]
            if session and session.get("prompt_cache_key"):
                payload["prompt_cache_key"] = session["prompt_cache_key"]
            if think_level in {"low", "medium", "high", "xhigh"}:
                payload["reasoning"] = {"effort": think_level}


    @classmethod
    def _apply_cache_projection(
        cls,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        provider: str,
        model: str,
    ) -> None:
        """Project Relay 3.0 implicit-prefix control independently of reasoning.

        The historical Grok path still computes/injects its key in
        ``_apply_reasoning``. The cache execution binding acts as an outer gate:
        effective None removes only the controllable key, while an approved
        implicit-prefix binding may supply the frozen key. 2.1/2.2 behavior is
        byte-for-byte unaffected by this hook.
        """
        if str(snapshot.get("schema_version") or "") not in V3_FENCED_REQUEST_VERSIONS:
            return
        binding = snapshot.get("_relay_cache_execution")
        if not isinstance(binding, dict):
            return
        mechanism = binding.get("mechanism")
        upgrade = snapshot.get("cache_upgrade") or {}
        if (upgrade.get("grok_mode") == "legacy_v1" and snapshot.get("requested_cache_mode") != "off"
                and (snapshot.get("cache_plan") or {}).get("tracks", {}).get("implicit", {}).get("legacy_allowed") is True):
            return
        profile = (snapshot.get("protocol_profile") or {}).get("cache") or {}
        affinity = affinity_hint()
        tracks = (binding.get("metadata") or {}).get("tracks") or {}
        if (upgrade and affinity and profile.get("prompt_cache_key_field") == "prompt_cache_key"
                and (mechanism == "implicit_prefix" or (tracks.get("implicit_base")
                     and profile.get("certified_implicit_hint") is True))):
            payload["prompt_cache_key"] = affinity
            return
        if mechanism is None:
            if cls._is_grok(provider, model):
                payload.pop("prompt_cache_key", None)
            return
        if mechanism != "implicit_prefix":
            return
        prefix = binding.get("prefix") if isinstance(binding.get("prefix"), dict) else {}
        key = prefix.get("prompt_cache_key")
        if key:
            payload["prompt_cache_key"] = str(key)

    @classmethod
    def _resolve_continuation_control(cls, snapshot: dict[str, Any]) -> tuple[bool, str | None]:
        """Resolve canonical Responses continuation state: (store, previous_response_id)."""
        ctrl = snapshot.get("_relay_responses_continuation")
        if isinstance(ctrl, dict):
            store_flag = bool(ctrl.get("store"))
            prev_id = ctrl.get("previous_response_id")
            return store_flag, (str(prev_id) if prev_id else None)

        binding = snapshot.get("_relay_continuation_execution") or snapshot.get("continuation_execution_binding")
        if isinstance(binding, dict):
            if binding.get("schema_version") == "relay-continuation-binding/2":
                eff = binding.get("effective_mode")
                store_flag = (binding.get("store_upstream_state") is True) and eff in {
                    "stateful_root_init",
                    "stateful_anchor_delta",
                }
                prev_id = binding.get("upstream_handle") if eff == "stateful_anchor_delta" else None
                return store_flag, (str(prev_id) if prev_id else None)
            if not binding.get("fallback_occurred") and binding.get("mechanism") not in (None, "none"):
                b_handle = binding.get("upstream_handle")
                if isinstance(b_handle, str) and b_handle.strip():
                    return True, b_handle
                if isinstance(b_handle, dict):
                    prev_id = (
                        b_handle.get("previous_response_id")
                        or b_handle.get("response_id")
                        or b_handle.get("id")
                    )
                    return True, (str(prev_id) if prev_id else None)

        if snapshot.get("normalized_head") is not None:
            return False, None
        prev_id = snapshot.get("previous_response_id")
        if isinstance(prev_id, str) and prev_id.strip():
            return False, prev_id
        return False, None

    @classmethod
    def _apply_model_options(cls, payload: dict[str, Any], snapshot: dict[str, Any]) -> None:
        if str(snapshot.get("schema_version") or "") in CANONICAL_REQUEST_VERSIONS:
            options = snapshot.get("effective_options") or {}
            if not isinstance(options, dict):
                return
            if "temperature" in options:
                payload["temperature"] = options["temperature"]
            if "top_p" in options:
                payload["top_p"] = options["top_p"]
            if "max_output_tokens" in options:
                payload["max_output_tokens"] = options["max_output_tokens"]
            return

        provider_payload = snapshot.get("provider_payload") or {}
        if isinstance(provider_payload, dict):
            for key, value in provider_payload.items():
                if key not in cls._PROTECTED_OVERRIDE_KEYS:
                    payload[key] = value

    @staticmethod
    def make_user_item(text: str) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }

    @classmethod
    def _material_items(cls, material_prefix: Any) -> list[Any]:
        if material_prefix is None:
            return []
        if isinstance(material_prefix, list):
            return material_prefix
        if isinstance(material_prefix, str):
            return [cls.make_user_item(material_prefix)]
        if isinstance(material_prefix, dict):
            maybe_input = material_prefix.get("input")
            if isinstance(maybe_input, list):
                return maybe_input
            # Already an input/message item.
            if "role" in material_prefix or "type" in material_prefix:
                return [material_prefix]
            # Generic wrapper for structured material supplied by Dify.
            return [
                cls.make_user_item(
                    "[Relay material prefix JSON]\n"
                    + json.dumps(material_prefix, ensure_ascii=False)
                )
            ]
        return [cls.make_user_item(str(material_prefix))]

    @staticmethod
    def _is_grok(provider: str, model: str) -> bool:
        return provider in {"grok", "xai"} or model.lower().startswith("grok-")

    @staticmethod
    def extract_visible_text(data: dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]

        pieces: list[str] = []
        output = data.get("output")
        if not isinstance(output, list):
            return ""

        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "output_text" and isinstance(item.get("text"), str):
                pieces.append(item["text"])

            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_text", "text"} and isinstance(
                    part.get("text"), str
                ):
                    pieces.append(part["text"])
        return "\n".join(piece for piece in pieces if piece)

    @staticmethod
    def extract_cached_tokens(usage: dict[str, Any]) -> int | None:
        candidates = [
            (usage.get("input_tokens_details") or {}).get("cached_tokens"),
            (usage.get("input_details") or {}).get("cached_tokens"),
            usage.get("cached_tokens"),
        ]
        for value in candidates:
            if isinstance(value, int):
                return value
        return None
