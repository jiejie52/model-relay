from __future__ import annotations

import json
from typing import Any

from .openai_compatible import OpenAICompatibleResponsesProvider
from .v2_base import V2ExecutionContext, V2ProviderResult
from ..materials.resolver import MaterialResolver


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

    async def execute(self, context: V2ExecutionContext) -> V2ProviderResult:
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
        for turn in context.history:
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
        return V2ProviderResult(
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
        )

    async def _material_prefix(self, context: V2ExecutionContext) -> list[Any]:
        items: list[Any] = []
        by_id = {str(x.get("material_id")): x for x in context.material_bindings}
        for material_id in context.material_ids:
            resolved = await self.materials.resolve(
                material_id,
                tenant_id=context.tenant_id,
                conversation_hash=context.conversation_hash,
                with_bytes=False,
            )
            mime = str(resolved.material.get("content_type") or "application/octet-stream")
            filename = str(resolved.material.get("filename") or material_id)
            frozen = by_id.get(material_id) or {}
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
                data = await self.materials.read_object(obj)
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
