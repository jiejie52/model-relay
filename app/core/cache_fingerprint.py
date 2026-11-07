"""Stable fingerprints for native prefix comparison, independent of full request identity."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .idempotency import stable_hash
from .context_normalizer import canonical_structure, serialize_wire


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


def _legacy_physical_chain(payload: dict[str, Any], *, scope: str) -> list[dict[str, Any]]:
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


def _legacy_lcp_diff(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    index = 0
    for before, after in zip(previous, current):
        if before.get("chained_prefix_hash") != after.get("chained_prefix_hash"):
            break
        index += 1
    identical = index == len(previous) == len(current)
    return {
        "lcp_index": index,
        "lcp_estimated_tokens": current[index - 1]["cumulative_estimated_tokens"] if index else 0,
        "first_divergence_index": None if identical else index + 1,
        "divergence_reason": "none" if identical else (
            "length_changed" if index == min(len(previous), len(current)) else "physical_segment_changed"
        ),
    }


def _wire_fragments(payload: dict[str, Any]):
    """Partition the *same* canonical JSON bytes, including every delimiter.

    Split only ordered messages/parts, not arbitrary schema arrays. Closing
    punctuation is separate so appending a turn retains all earlier blocks.
    """
    ordered = json.loads(serialize_wire(payload))

    def encoded(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def walk(value, path, split=False):
        if isinstance(value, dict) and split:
            yield "wire_structure", path, b"{", None
            for index, (key, item) in enumerate(value.items()):
                yield "wire_structure", path, (b"," if index else b"") + encoded(key) + b":", None
                nested = key in {"contents", "messages", "input", "parts", "content"} and isinstance(item, (list, dict))
                yield from walk(item, f"{path}.{key}", nested)
            yield "wire_structure", path, b"}", None
        elif isinstance(value, list) and split:
            yield "wire_structure", path, b"[", None
            for index, item in enumerate(value):
                if index:
                    yield "wire_structure", path, b",", None
                # Whole Part objects are the semantic/material boundary.
                is_part = path.endswith((".parts", ".content"))
                yield from walk(item, f"{path}[{index}]", isinstance(item, dict) and not is_part)
            yield "wire_structure", path, b"]", None
        else:
            yield "content", path, encoded(value), value

    yield from walk(ordered, "$", True)


def physical_chain(payload: dict[str, Any], *, scope: str, version: int = 1,
                   material_identities: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if version < 2:
        return _legacy_physical_chain(payload, scope=scope)
    identities = material_identities or {}
    chain = []
    parent = stable_hash({"scope": scope, "format": "relay-physical-chain/2"})
    cumulative = 0
    offset = 0
    for kind, path, raw, value in _wire_fragments(payload):
        local = hashlib.sha256(raw).hexdigest()
        semantic = stable_hash(value) if kind != "wire_structure" else local
        source_ref = None
        material = identities.get(stable_hash(value)) if isinstance(value, dict) else None
        if material:
            semantic = stable_hash({"material_id": material["material_id"],
                                    "content": material["sha256"], "mime": material["content_type"],
                                    "projection": material.get("projection_sha256")})
            kind = material["class"]
            source_ref = material["material_id"]
        elif path.startswith("$.systemInstruction") or path.startswith("$.instructions") or path.startswith("$.system"):
            kind = "sys_instr" if kind != "wire_structure" else kind
        elif path.startswith("$.tools"):
            kind = "tool_schema" if kind != "wire_structure" else kind
        count = estimated_tokens(value) if kind != "wire_structure" and not path.endswith((
            ".role", ".model", ".type", ".cachedContent", ".previous_response_id"
        )) else 0
        cumulative += count
        parent = stable_hash([parent, local])
        chain.append({"format": "relay-physical-chain/2", "index": len(chain) + 1,
                      "kind": kind, "source_ref": source_ref, "path": path,
                      "semantic_hash": semantic, "local_physical_hash": local,
                      "chained_prefix_hash": parent, "estimated_tokens": count,
                      "cumulative_estimated_tokens": cumulative, "raw_byte_size": len(raw),
                      "byte_start": offset, "byte_end": offset + len(raw)})
        offset += len(raw)
    return chain


def compute_lcp_diff(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    v2 = bool(current and current[0].get("format") == "relay-physical-chain/2")
    if not v2:
        return _legacy_lcp_diff(previous, current)
    comparable = bool(previous and previous[0].get("format") == "relay-physical-chain/2")
    before = previous if comparable else []
    index = 0
    for a, b in zip(before, current):
        if a.get("chained_prefix_hash") != b.get("chained_prefix_hash"):
            break
        index += 1
    identical = comparable and index == len(before) == len(current)
    reason = "none" if identical else "baseline_unavailable" if not comparable else "content_changed"
    if comparable and not identical:
        if index == min(len(before), len(current)):
            reason = "length_extended" if len(current) > len(before) else "length_changed"
        elif before[index].get("semantic_hash") == current[index].get("semantic_hash"):
            reason = "wire_projection_changed"
    prefix = current[index - 1] if index else {}
    tokens = prefix.get("cumulative_estimated_tokens", 0)
    return {"lcp_index": index, "lcp_estimated_tokens": tokens,
            "lcp_raw_bytes": prefix.get("byte_end", 0),
            "lcp_chained_hash": prefix.get("chained_prefix_hash"),
            "first_divergence_index": None if identical else index + 1,
            "divergence_reason": reason, "comparable": comparable,
            "suffix_estimated_tokens": max(0, current[-1]["cumulative_estimated_tokens"] - tokens) if current else 0}
