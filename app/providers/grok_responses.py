from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx

from ..config import Settings
from ..structured_output import StructuredOutputError, apply_openai_responses_structured_output
from ..utils import utcnow
from .base import ProviderRequestError, ProviderResult
from .openai_compatible import OpenAICompatibleResponsesProvider
from .v2_common import decode_http_body, response_http_error, send_raw, transport_error, wire_hash


class GrokResponsesAdapter:
    provider = "grok"
    protocol = "responses"
    history_codec = "grok-responses-native"
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
        input_items: list[Any] = []
        for message in context:
            input_items.extend(await self._encode_message(message, material_resolver, session))
        if isinstance(history, list):
            for turn in history:
                if not isinstance(turn, dict):
                    continue
                user_items = turn.get("user_items")
                if isinstance(user_items, list):
                    input_items.extend(user_items)
                output = turn.get("response_output")
                if isinstance(output, list):
                    input_items.extend(output)

        current_items: list[Any] = []
        for message in request_snapshot.get("input") or []:
            encoded = await self._encode_message(message, material_resolver, session)
            input_items.extend(encoded)
            current_items.extend(encoded)

        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        cache_key = str(session.get("context_hash") or session.get("material_set_hash") or "").strip()
        if cache_key:
            payload["prompt_cache_key"] = f"relay-v2-{cache_key[:48]}"
        applied_generation = self._apply_generation(payload, request_snapshot.get("generation") or {}, model)
        try:
            apply_openai_responses_structured_output(
                payload,
                request_snapshot,
                fallback_name="relay_output",
                provider="grok",
                model=model,
            )
        except StructuredOutputError as exc:
            raise ProviderRequestError(exc.code, exc.message) from exc

        h = wire_hash(payload)
        await before_dispatch(h)
        if self.settings.aihubmix_api_key is None:
            raise ProviderRequestError("PROVIDER_CREDENTIAL_MISSING", "AIHUBMIX_API_KEY is not configured")
        headers = {
            "Authorization": f"Bearer {self.settings.aihubmix_api_key.get_secret_value()}",
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
        url = f"{self.profile.api_origin.rstrip('/')}/responses"
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=True, follow_redirects=False) as client:
                response, raw = await send_raw(client, "POST", url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise transport_error(exc, provider=self.provider, service="responses") from exc
        if not 200 <= response.status_code < 300:
            raise response_http_error(response, raw, provider=self.provider, service="responses")
        try:
            decoded = decode_http_body(raw, response.headers.get("content-encoding"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            from .base import ProviderHTTPError
            raise ProviderHTTPError(
                response.status_code,
                raw,
                "Grok Responses body was not valid JSON",
                headers=list(response.headers.multi_items()),
                content_type=response.headers.get("content-type"),
                content_encoding=response.headers.get("content-encoding"),
                provider=self.provider,
                service="responses",
            ) from exc
        text = OpenAICompatibleResponsesProvider.extract_visible_text(data)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        output = data.get("output") if isinstance(data.get("output"), list) else []
        return ProviderResult(
            raw_bytes=raw,
            raw_json=data,
            text=text,
            response_id=data.get("id"),
            usage=usage,
            cached_tokens=OpenAICompatibleResponsesProvider.extract_cached_tokens(usage),
            response_output=output,
            history_delta={"user_items": current_items, "response_output": output},
            finish_reason=str(data.get("status")) if data.get("status") is not None else None,
            result_type="message",
            wire_request_hash=h,
            applied_generation=applied_generation,
        )

    def decode_archived_v2(self, request_snapshot: dict[str, Any], archived: dict[str, Any]) -> ProviderResult:
        return ProviderResult(**archived)

    async def _encode_message(self, message: dict[str, Any], resolver: Any, session: dict[str, Any]) -> list[Any]:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, str):
            return [{"role": role, "content": [{"type": "input_text", "text": content}]}]
        if not isinstance(content, list):
            return [{"role": role, "content": [{"type": "input_text", "text": str(content or "")}]}]

        parts: list[dict[str, Any]] = []
        transport_epoch = 0
        for part in content:
            if not isinstance(part, dict):
                parts.append({"type": "input_text", "text": str(part)})
                continue
            ptype = str(part.get("type") or "")
            if ptype == "text":
                parts.append({"type": "input_text", "text": str(part.get("text") or "")})
                continue
            if ptype != "material_ref":
                if "text" in part:
                    parts.append({"type": "input_text", "text": str(part.get("text") or "")})
                    continue
                raise ProviderRequestError("GROK_CONTENT_UNSUPPORTED", f"Unsupported content part: {ptype or '<empty>'}")

            material_id = str(part.get("material_id") or "")
            metadata = await resolver.metadata(material_id)
            source_ref = str(part.get("source_ref") or metadata.get("filename") or material_id)
            mime = str(metadata.get("detected_mime") or metadata.get("declared_mime") or "application/octet-stream").lower()
            binding = await resolver.repository.get_material_binding(
                material_id=material_id,
                provider=self.provider,
                upstream_profile=str(session["upstream_profile"]),
                account_scope=str(session["account_scope"]),
                purpose="responses-url",
                transform_version="grok-presigned-url-v1",
            )
            from .v2_common import parse_expiry
            binding_expiry = parse_expiry((binding or {}).get("expires_at"))
            if (
                binding
                and binding.get("status") == "ready"
                and binding.get("native_uri")
                and binding_expiry is not None
                and binding_expiry > utcnow() + timedelta(seconds=self.settings.binding_safety_window)
            ):
                url = str(binding["native_uri"])
                transport_epoch = int(binding.get("transport_epoch") or 0)
            else:
                # Sign immediately before request preparation; canonical material
                # identity remains stable even when transport credentials change.
                try:
                    binding_claim = await resolver.claim_binding(
                        material=metadata,
                        provider=self.provider,
                        upstream_profile=str(session["upstream_profile"]),
                        account_scope=str(session["account_scope"]),
                        protocol=self.protocol,
                        capability_group="grok-url",
                        purpose="responses-url",
                        transform_version="grok-presigned-url-v1",
                        lease_seconds=max(120, int(self.settings.provider_fetch_url_ttl)),
                    )
                except TimeoutError as exc:
                    raise ProviderRequestError("PROVIDER_BINDING_BUSY", str(exc)) from exc
                except ValueError as exc:
                    raise ProviderRequestError("PROVIDER_BINDING_RESERVATION_FAILED", str(exc)) from exc
                try:
                    url = await resolver.presign(material_id, expires_seconds=self.settings.provider_fetch_url_ttl)
                    transport_epoch = int((binding or {}).get("transport_epoch") or -1) + 1
                    try:
                        await resolver.complete_binding(
                            binding_claim,
                            native_file_id=None,
                            native_uri=url,
                            derived_object_path=None,
                            expires_at=(utcnow() + timedelta(seconds=self.settings.provider_fetch_url_ttl)).isoformat(),
                            transport_epoch=transport_epoch,
                            wire_request_hash=None,
                            cache_identity=f"grok-url:{material_id}:{transport_epoch}",
                        )
                    except ValueError as exc:
                        raise ProviderRequestError("PROVIDER_BINDING_FENCE_LOST", str(exc)) from exc
                except Exception:
                    await resolver.fail_binding(binding_claim)
                    raise
            parts.append({"type": "input_text", "text": f"[SOURCE_FILE:{source_ref}]"})
            if mime.startswith("image/"):
                parts.append({"type": "input_image", "image_url": url})
            else:
                parts.append({"type": "input_file", "file_url": url})
            parts.append({"type": "input_text", "text": f"[END_SOURCE_FILE:{source_ref}]"})
        return [{"role": role, "content": parts}]

    def _apply_generation(self, payload: dict[str, Any], generation: dict[str, Any], model: str) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        reasoning = generation.get("reasoning") if isinstance(generation.get("reasoning"), dict) else {}
        effort = str(reasoning.get("effort") or "auto").lower()
        if effort != "auto":
            allowed = {"low", "medium", "high"}
            if model.lower().startswith("grok-4.6"):
                allowed.add("xhigh")
            if effort not in allowed:
                raise ProviderRequestError("MODEL_REASONING_CONFIG_INVALID", f"Unsupported reasoning effort {effort} for {model}")
            payload["reasoning"] = {"effort": effort}
            applied["reasoning"] = {"effort": effort}
        for key in ("temperature", "top_p", "max_output_tokens"):
            if key in generation:
                payload[key] = generation[key]
                applied[key] = generation[key]
        return applied
