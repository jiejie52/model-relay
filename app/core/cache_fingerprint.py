"""Stable fingerprints for native prefix comparison, independent of full request identity."""
from __future__ import annotations

from typing import Any

from .idempotency import stable_hash


def estimated_tokens(value: Any) -> int:
    # Conservative lexical estimate. Never use Base64 or file URI bytes as tokens.
    if isinstance(value, str):
        return len(value.encode("utf-8")) // 6
    if isinstance(value, list):
        return sum(estimated_tokens(item) for item in value)
    if isinstance(value, dict):
        return sum(estimated_tokens(item) for key, item in value.items() if key not in {
            "fileData", "inlineData", "data", "file_id", "file_uri", "image_url", "signature",
            "thoughtSignature", "encrypted_content", "source", "role", "type", "id",
        })
    return 0


def physical_chain(payload: dict[str, Any], *, scope: str) -> list[dict[str, Any]]:
    segments: list[tuple[str, Any]] = []
    for key in ("model", "cachedContent", "previous_response_id", "systemInstruction", "system", "instructions",
                "tools", "toolConfig", "tool_choice", "thinking", "output_config", "generationConfig",
                "response_format", "text", "reasoning", "safetySettings"):
        if key in payload:
            segments.append((key, payload[key]))
    for key in ("contents", "messages", "input"):
        value = payload.get(key)
        if isinstance(value, list):
            segments.extend((key, item) for item in value)
        elif value is not None:
            segments.append((key, value))
    chain = []
    parent = stable_hash({"scope": scope, "format": "relay-physical-chain/1"})
    cumulative = 0
    for kind, value in segments:
        local = stable_hash([kind, value])
        cumulative += estimated_tokens(value) if kind not in {"model", "cachedContent", "previous_response_id"} else 0
        parent = stable_hash([parent, local])
        chain.append({"kind": kind, "local_physical_hash": local,
                      "chained_prefix_hash": parent, "cumulative_estimated_tokens": cumulative})
    return chain


def compute_lcp_diff(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    index = 0
    for before, after in zip(previous, current):
        if before.get("chained_prefix_hash") != after.get("chained_prefix_hash"):
            break
        index += 1
    identical = index == len(previous) == len(current)
    if any(node.get("chain_version") == 2 for node in current[:1] + previous[:1]):
        reason = "none"
        if not identical:
            if index == len(previous) and len(current) > len(previous):
                reason = "length_extended"
            elif index == len(current) and len(previous) > len(current):
                reason = "length_truncated"
            elif previous[index].get("semantic_hash") == current[index].get("semantic_hash"):
                reason = "wire_projection_changed"
            else:
                reason = "content_changed"
        tokens = current[index - 1]["cumulative_estimated_tokens"] if index else 0
        return {"lcp_index": index, "lcp_estimated_tokens": tokens,
                "first_divergence_index": None if identical else index + 1,
                "divergence_reason": reason,
                "suffix_estimated_tokens": (current[-1]["cumulative_estimated_tokens"] if current else 0) - tokens}
    return {
        "lcp_index": index,
        "lcp_estimated_tokens": current[index - 1]["cumulative_estimated_tokens"] if index else 0,
        "first_divergence_index": None if identical else index + 1,
        "divergence_reason": "none" if identical else (
            "length_changed" if index == min(len(previous), len(current)) else "physical_segment_changed"
        ),
    }


def physical_chain_v2(payload: dict[str, Any], *, scope: str,
                      material_bindings: list[dict[str, Any]] | None = None,
                      history: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Exact canonical native-block representations, not TCP/HTTP byte offsets.

    No content is moved. The full HTTP entity digest remains separate. Only
    verified Material transport references may be substituted in semantic hashes.
    """
    import base64
    import hashlib
    from .idempotency import canonical_json_bytes

    references: dict[str, Any] = {}
    for binding in material_bindings or []:
        if not binding.get("material_id") or not binding.get("content_sha256"):
            continue
        identity = {k: binding.get(k) for k in ("material_id", "content_sha256", "content_type", "filename")}
        for key in ("external_uri", "file_uri", "uri", "signed_url", "url", "file_id", "provider_file_id"):
            value = binding.get(key)
            if isinstance(value, str) and value:
                references[value] = identity

    def semantic(value: Any, key: str = "") -> Any:
        if isinstance(value, str) and key in {"fileUri", "file_uri", "file_url", "image_url", "file_id"}:
            return references.get(value, value)
        if isinstance(value, list):
            return [semantic(x) for x in value]
        if isinstance(value, dict):
            if key in {"image_url", "video_url"}:
                # Chat-style media URLs are nested; ordinary caller JSON url
                # fields are not transport references and must remain literal.
                return {k: references.get(v, v) if k == "url" and isinstance(v, str)
                        else semantic(v, k) for k, v in value.items()}
            if key == "inlineData" and isinstance(value.get("data"), str):
                try:
                    raw = base64.b64decode(value["data"], validate=True)
                    return {"mimeType": value.get("mimeType"), "content_sha256": hashlib.sha256(raw).hexdigest()}
                except (ValueError, TypeError):
                    pass
            return {k: semantic(v, k) for k, v in value.items()}
        return value

    historical = []
    for i, turn in enumerate(history or []):
        t = turn.get("transport_history") or {}
        blocks = []
        for key in ("user_content", "user_message", "user_item"):
            if isinstance(t.get(key), dict):
                blocks.append(t[key])
        for key in ("model_content", "assistant_message"):
            if isinstance(t.get(key), dict):
                blocks.append(t[key])
        if isinstance(t.get("response_output"), list):
            blocks.extend(t["response_output"])
        historical.extend((stable_hash(x), i + 1, j == len(blocks) - 1) for j, x in enumerate(blocks))

    blocks = []
    # Non-message dependencies are retained conservatively, never guessed away.
    for key, value in payload.items():
        if key not in {"contents", "messages", "input"}:
            kind = "sys_instr" if key in {"systemInstruction", "system", "instructions"} else (
                "tool_schema" if key in {"tools", "response_format", "text"} else "native_dependency")
            blocks.append((key, kind, value, key, True))
    history_index = 0
    for key in ("contents", "messages", "input"):
        values = payload.get(key)
        if values is None:
            continue
        for value in values if isinstance(values, list) else [values]:
            source, safe, kind = key, True, "dynamic_suffix"
            if history_index < len(historical) and stable_hash(value) == historical[history_index][0]:
                _, turn, safe = historical[history_index]
                source, kind = f"history:{turn}", "history_turn"
                history_index += 1
            elif isinstance(value, dict) and value.get("role") in {"system", "developer"}:
                kind = "sys_instr"
            part_key = next((name for name in ("parts", "content")
                             if isinstance(value, dict) and isinstance(value.get(name), list)), None)
            if part_key and value[part_key]:
                # The header and every Part retain their original position.
                # Individual material nodes are visible, but only the complete
                # message/turn end is an eligible controlled reuse boundary.
                blocks.append((key, kind, {k: v for k, v in value.items() if k != part_key}, source, False))
                for part_index, part in enumerate(value[part_key]):
                    mime = str((part.get("inlineData") or part.get("fileData") or {}).get("mimeType") or "") if isinstance(part, dict) else ""
                    part_kind = ("material_pdf" if mime == "application/pdf" else "material_video"
                                 if mime.startswith("video/") else "material_docx_proj"
                                 if "wordprocessingml" in mime else "material_other" if mime else kind)
                    blocks.append((f"{key}.{part_key}", part_kind, part, f"{source}:part:{part_index}",
                                   safe and part_index == len(value[part_key]) - 1))
            else:
                blocks.append((key, kind, value, source, safe))
    parent = stable_hash({"scope": scope, "format": "relay-physical-chain/2"})
    chain, cumulative = [], 0
    for key, kind, value, source, safe in blocks:
        raw = canonical_json_bytes([key, value])
        local = hashlib.sha256(raw).hexdigest()
        parent = stable_hash([parent, local])
        count = estimated_tokens(value) if key not in {"model", "cachedContent", "previous_response_id"} else 0
        cumulative += count
        chain.append({"chain_version": 2, "index": len(chain) + 1, "kind": key, "seg_type": kind,
                      "representation_version": "native-block-json/2",
                      "semantic_hash": stable_hash([key, semantic(value)]), "local_physical_hash": local,
                      "chained_prefix_hash": parent, "estimated_tokens": count,
                      "cumulative_estimated_tokens": cumulative, "raw_byte_size": len(raw),
                      "source_ref": source, "safe_boundary": safe})
    return chain
