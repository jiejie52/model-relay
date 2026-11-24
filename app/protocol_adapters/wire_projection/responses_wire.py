"""Deterministic wire projection helpers for OpenAI / Grok Responses materials."""
from __future__ import annotations

import base64
import json
from typing import Any

from app.core.context_codec import source_markers
from app.materials.resolver import MaterialResolver

RESPONSES_FALLBACK_ADAPTER_VERSION = "responses-fallback/1"
RESPONSES_TEXT_MIMES = frozenset({"application/json", "application/xml", "text/markdown"})


def responses_input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def project_responses_current_user_item(
    snapshot: dict[str, Any],
    *,
    material_ids: list[str],
    material_bindings: list[dict[str, Any]],
    materials: MaterialResolver,
    tenant_id: str,
    conversation_hash: str,
) -> dict[str, Any]:
    """Build the single chronological Responses user item with 1 part per material + prompt.

    Returns ``{"role": "user", "content": [*material_parts, {"type": "input_text", "text": query}]}``.
    The part order matches ``material_ids`` 1:1 so ``part_index`` and ``part_digest``
    align across PhysicalContextProjector, verify_pre_seal_material_invariants,
    ResponsesV2Adapter, and ResultCommitter.
    """
    content: list[dict[str, Any]] = []
    by_id = {str(b.get("material_id")): b for b in (material_bindings or ())}
    for material_id in material_ids:
        mid = str(material_id)
        frozen = by_id.get(mid) or {}
        resolved = await materials.resolve_frozen(
            frozen, tenant_id=tenant_id, conversation_hash=conversation_hash, with_bytes=False
        )
        mime = str(resolved.material.get("content_type") or "application/octet-stream").split(";", 1)[0].strip().lower()
        filename = str(resolved.material.get("filename") or mid)

        if frozen.get("projection_object_id"):
            projected = await materials.read_bound_text(
                frozen, tenant_id=tenant_id, conversation_hash=conversation_hash
            )
            if projected is not None:
                begin, end = source_markers(mid, filename, upgraded=True)
                content.append({
                    "type": "input_text",
                    "text": f"{begin}\n{projected.decode('utf-8')}\n{end}",
                })
                continue

        if mime.startswith("text/") or mime in RESPONSES_TEXT_MIMES:
            with_data = await materials.resolve_frozen(
                frozen, tenant_id=tenant_id, conversation_hash=conversation_hash, with_bytes=True
            )
            text = (with_data.data or b"").decode("utf-8", errors="replace")
            content.append({
                "type": "input_text",
                "text": f"[material {mid}: {filename}]\n{text}",
            })
            continue

        representation = str(frozen.get("representation") or "")
        ext_uri = str(frozen.get("external_uri") or "")
        if representation == "responses_external_url" and ext_uri.startswith(("http://", "https://")):
            if mime.startswith("image/"):
                content.append({"type": "input_image", "image_url": ext_uri})
            else:
                content.append({"type": "input_file", "file_url": ext_uri})
            continue

        # Default deterministic inline projection per official OpenAI/xAI Responses specs:
        # images -> input_image data URI; documents -> input_file with filename + data URI.
        with_data = await materials.resolve_frozen(
            frozen, tenant_id=tenant_id, conversation_hash=conversation_hash, with_bytes=True
        )
        encoded = base64.b64encode(with_data.data or b"").decode("ascii")
        data_uri = f"data:{mime};base64,{encoded}"
        if mime.startswith("image/"):
            content.append({"type": "input_image", "image_url": data_uri})
        else:
            content.append({
                "type": "input_file",
                "filename": filename,
                "file_data": data_uri,
            })

    content.append({"type": "input_text", "text": responses_input_text(snapshot.get("input"))})
    return {"role": "user", "content": content}


def project_responses_head(payload, snapshot, parent_id):
    """Project the already frozen head according to the supplied protocol profile."""
    head = snapshot.get("normalized_head")
    if head is None:
        return
    instructions = head["instructions"]
    if instructions:
        layout = head["instructions_layout"]
        if layout == "per_request":
            payload["instructions"] = instructions
        elif layout == "root_system_message":
            if parent_id is None:
                payload["input"].insert(0, {"role": "system", "content": instructions})
        else:
            from app.protocol_adapters.base import ProviderRequestError
            raise ProviderRequestError("RESPONSES_HEAD_LAYOUT_INVALID", "Unsupported fixed-head layout")
    if head["tools"]:
        payload["tools"] = head["tools"]


def observe_responses_outcome(data):
    """Use native Responses facts for inference, probe qualification and commit recovery."""
    if not isinstance(data, dict):
        return {"completion": "unknown", "status": None, "reason": "invalid_response_object",
                "pending_client_tool": False, "refusal": False}
    status_value = data.get("status")
    status = status_value if isinstance(status_value, str) else None
    details = data.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if data.get("error") or status in {"incomplete", "failed", "cancelled"}:
        completion = "failed"
    elif status == "completed" and isinstance(data.get("output"), list):
        completion = "completed"
    else:
        completion = "unknown"
    output = data.get("output") if isinstance(data.get("output"), list) else []
    return {
        "completion": completion, "status": status, "reason": reason,
        "pending_client_tool": any(isinstance(item, dict) and item.get("type") in {
            "function_call", "custom_tool_call", "mcp_approval_request"} for item in output),
        "refusal": any(isinstance(part, dict) and part.get("type") == "refusal"
                       for item in output if isinstance(item, dict)
                       for part in (item.get("content") if isinstance(item.get("content"), list) else [])),
    }
