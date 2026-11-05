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
    return {
        "lcp_index": index,
        "lcp_estimated_tokens": current[index - 1]["cumulative_estimated_tokens"] if index else 0,
        "first_divergence_index": None if identical else index + 1,
        "divergence_reason": "none" if identical else (
            "length_changed" if index == min(len(previous), len(current)) else "physical_segment_changed"
        ),
    }
