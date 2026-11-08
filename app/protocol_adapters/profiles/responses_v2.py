from __future__ import annotations

import json
from typing import Any

from app.protocol_adapters.profiles.openai_compatible import OpenAICompatibleResponsesProvider
from app.protocol_adapters.v2_base import SealedDispatchBundle, StandardizedProviderResult
from app.protocol_adapters.wire_projection.http_wire import observed_response_facts
from app.materials.resolver import MaterialResolver


class ResponsesV2Adapter:
    """v2 wrapper for the existing OpenAI-compatible Responses adapter.

    It keeps provider wire differences inside the adapter while v2 Session/
    Request code only deals with material_id and generic input.
    """

    adapter_version = "responses-v2/2"

    def __init__(
        self,
        legacy: OpenAICompatibleResponsesProvider,
        materials: MaterialResolver,
    ) -> None:
        self.legacy = legacy
        self.materials = materials

    async def execute(self, context: SealedDispatchBundle) -> StandardizedProviderResult:
        context.verify_integrity()
        snapshot = dict(context.snapshot)
        snapshot["_relay_log_context"] = {
            "request_id": context.request_id,
            "session_id": context.session_id,
            "provider": snapshot.get("provider"),
            "connection_id": snapshot.get("connection_id"),
            "model": snapshot.get("model"),
        }
        snapshot["current_query"] = self._input_text(snapshot.get("input"))
        snapshot["mode"] = "continue_session"
        snapshot["material_prefix_includes_current_query"] = False
        material_prefix = await self._material_prefix(context)

        legacy_history = []
        for turn in (context.history if snapshot.get("history_mode", "cumulative") != "ephemeral_turn" else []):
            transport = turn.get("transport_history") if isinstance(turn, dict) else None
            if isinstance(transport, dict) and transport.get("kind") == "responses":
                legacy_history.append(
                    {
                        "user_item": transport.get("user_item"),
                        "response_output": transport.get("response_output") or [],
                    }
                )

        result = await self.legacy.execute(
            snapshot,
            session=context.session,
            material_prefix=material_prefix,
            history=legacy_history,
        )
        user_item = self.legacy.make_user_item(snapshot["current_query"])
        return StandardizedProviderResult(
            raw_bytes=result.raw_bytes,
            raw_json=result.raw_json,
            text=result.text,
            response_id=result.response_id,
            usage=result.usage,
            cached_tokens=result.cached_tokens,
            response_output=result.response_output,
            history_entry={
                "transport_history": {
                    "kind": "responses",
                    "user_item": user_item,
                    "response_output": result.response_output,
                }
            },
            http_status=result.http_status,
            provider_request_id=result.provider_request_id,
            observed=observed_response_facts(
                result.response_headers,
                body_model=(str(result.raw_json.get("model")) if result.raw_json.get("model") else None),
                protocol="responses",
                channel_id=(str(snapshot.get("channel_id")) if snapshot.get("channel_id") else None),
            evidence_headers=(((context.snapshot.get("protocol_profile") or {}).get("metadata") or {}).get("response_evidence_headers")),
            ),
        )

    async def _material_prefix(self, context: SealedDispatchBundle) -> list[Any]:
        items: list[Any] = []
        by_id = {str(x.get("material_id")): x for x in context.material_bindings}
        for material_id in context.material_ids:
            frozen = by_id.get(material_id) or {}
            if context.snapshot.get("schema_version") == "relay-request/2.4":
                resolved = await self.materials.resolve_frozen(frozen, tenant_id=context.tenant_id,
                    conversation_hash=context.conversation_hash, with_bytes=False)
            else:
                resolved = await self.materials.resolve(
                    material_id,
                    tenant_id=context.tenant_id,
                    conversation_hash=context.conversation_hash,
                    with_bytes=False,
                )
            mime = str(resolved.material.get("content_type") or "application/octet-stream")
            filename = str(resolved.material.get("filename") or material_id)
            frozen = by_id.get(material_id) or {}
            projected = await self.materials.read_bound_text(frozen, tenant_id=context.tenant_id,
                                                             conversation_hash=context.conversation_hash) if frozen.get("projection_object_id") else None
            if projected is not None:
                from app.core.context_codec import source_markers
                begin, end = source_markers(material_id, filename, upgraded=True)
                items.append(self.legacy.make_user_item(f"{begin}\n{projected.decode('utf-8')}\n{end}"))
                continue
            object_id = frozen.get("object_id")
            obj = None
            if object_id:
                obj = await self.materials.repo.get_object(
                    str(object_id),
                    tenant_id=context.tenant_id,
                    conversation_hash=context.conversation_hash,
                )
            if obj is None:
                obj = resolved.object
            if obj is None:
                raise LookupError(f"material has no fallback object for Responses transport: {material_id}")
            if mime.startswith("text/") or mime in {
                "application/json",
                "application/xml",
                "text/markdown",
            }:
                data = (await self.materials.resolve_frozen(frozen, tenant_id=context.tenant_id,
                    conversation_hash=context.conversation_hash, with_bytes=True)).data if context.snapshot.get("schema_version") == "relay-request/2.4" else await self.materials.read_object(obj)
                text = data.decode("utf-8", errors="replace")
                items.append(
                    self.legacy.make_user_item(
                        f"[material {material_id}: {filename}]\n{text}"
                    )
                )
                continue

            signed = await self.materials.sign_object(obj, expires_in=3600)
            if mime.startswith("image/"):
                content = [
                    {"type": "input_text", "text": f"Material {material_id}: {filename}"},
                    {"type": "input_image", "image_url": signed},
                ]
            else:
                content = [
                    {"type": "input_text", "text": f"Material {material_id}: {filename}"},
                    {"type": "input_file", "file_url": signed},
                ]
            items.append({"role": "user", "content": content})
        return items

    @staticmethod
    def _input_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
