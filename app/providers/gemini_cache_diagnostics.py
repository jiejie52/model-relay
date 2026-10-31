"""Non-content-bearing diagnostics for the final Gemini wire request.

These observations never modify admission, Provider payloads or usage semantics.
All fingerprints are internal comparisons, not Provider cache keys or hit proof.
"""
from __future__ import annotations
from collections import Counter
from typing import Any
from ..core.idempotency import stable_hash

VERSION = "relay-gemini-cache-diagnostics/1"


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _hash(value: Any) -> str | None:
    return stable_hash(value) if value is not None else None


def request_facts(payload: dict, *, snapshot: dict, physical: dict | None,
                  session: dict) -> dict:
    physical = _dict(physical)
    generation = _dict(payload.get("generationConfig"))
    contents = payload.get("contents") if isinstance(payload.get("contents"), list) else []
    # Bounded item details; complete content hash remains available for large histories.
    items = []
    for content in contents[:32]:
        content = _dict(content)
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        kinds = Counter()
        for part in parts:
            part = _dict(part)
            for key in ("text", "inlineData", "fileData", "functionCall", "functionResponse", "thoughtSignature"):
                if key in part:
                    kinds[key] += 1
        role = content.get("role")
        items.append({"role": role if role in {"user", "model"} else "other",
                      "hash": stable_hash(content), "part_types": dict(kinds)})
    dependencies = _dict(physical.get("dependencies"))
    prefix = _dict(physical.get("cached_prefix"))
    material_prefix = prefix.get("contents")
    material_present = bool(dependencies.get("cache_material_ids"))
    material_content = material_prefix[0] if material_present and isinstance(material_prefix, list) and material_prefix else None
    route = _dict(_dict(session.get("metadata")).get("_relay_route"))
    sampling = {k:v for k,v in generation.items()
                if k not in {"responseSchema", "responseJsonSchema", "responseMimeType", "thinkingConfig"}}
    return {
        "schema_version": VERSION,
        "wire_payload_hash": stable_hash(payload),
        "wire_system_instruction_hash": _hash(payload.get("systemInstruction")),
        "wire_contents_hash": stable_hash(contents),
        "wire_generation_config_hash": stable_hash(generation),
        "wire_sampling_hash": stable_hash(sampling),
        "wire_thinking_hash": _hash(generation.get("thinkingConfig")),
        "wire_schema_hash": _hash(generation.get("responseSchema", generation.get("responseJsonSchema"))),
        "wire_response_mime_hash": _hash(generation.get("responseMimeType")),
        "wire_tools_hash": _hash(payload.get("tools")),
        "wire_tool_config_hash": _hash(payload.get("toolConfig")),
        "wire_safety_settings_hash": _hash(payload.get("safetySettings")),
        "wire_cached_content_present": bool(payload.get("cachedContent")),
        "wire_cached_content_handle_hash": _hash(payload.get("cachedContent")),
        "wire_content_count": len(contents), "wire_content_items": items,
        "wire_content_items_truncated": len(contents) > len(items),
        # This is a projection hash, including for stateful requests where it is NOT sent again.
        "material_projection_content_hash": _hash(material_content),
        "material_count": len(dependencies.get("cache_material_ids") or []),
        "material_fingerprint": _dict(_dict(snapshot.get("context_plan")).get("material_reuse")).get("ordered_material_fingerprint"),
        "safe_history_prefix_version": dependencies.get("safe_history_prefix_version"),
        "layout_version": physical.get("layout_version"),
        "connection_id": snapshot.get("connection_id"),
        "offering_id": snapshot.get("offering_id"),
        "protocol_profile_hash": snapshot.get("protocol_profile_hash"),
        "account_scope_hash": route.get("account_scope_hash"),
        "fingerprints_are_provider_cache_keys": False,
    }


def _counter(body: dict, path: str) -> dict:
    value: Any = body
    present = True
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            present = False
            value = None
            break
        value = value[key]
    valid = type(value) is int and value >= 0
    return {"path": path, "present": present,
            "type": type(value).__name__ if present else None,
            "value": value if valid else None}


def response_facts(body: dict, headers: dict) -> dict:
    counters = [_counter(body, path) for path in (
        "usageMetadata.cachedContentTokenCount",
        "usage_metadata.cached_content_token_count",
        "usage.input_tokens_details.cached_tokens",
        "usage.prompt_tokens_details.cached_tokens",
        "usage.cached_tokens",
    )]
    primary = counters[0]
    if primary["value"] is not None:
        reason = "provider_cached_tokens_positive" if primary["value"] > 0 else "provider_zero_cached_tokens"
    elif primary["present"]:
        reason = "provider_cache_counter_invalid"
    else:
        reason = "provider_cache_counter_missing"
    meta = _dict(body.get("usageMetadata"))
    allowed = {"promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount",
               "totalTokenCount", "thoughtsTokenCount", "toolUsePromptTokenCount",
               "promptTokensDetails", "cacheTokensDetails", "candidatesTokensDetails", "trafficType"}
    scalar = {k: meta[k] for k in allowed if k in meta and type(meta[k]) is int and meta[k] >= 0}
    lowered = {str(k).lower(): v for k, v in headers.items()}
    header_hashes = {k: stable_hash(lowered[k]) for k in (
        "x-request-id", "request-id", "x-aihubmix-request-id", "x-aihubmix-tid", "tid",
        "x-goog-request-id", "x-goog-region", "server", "x-aihubmix-final-model") if k in lowered}
    return {
        "usage_metadata_present": "usageMetadata" in body,
        "usage_metadata_type": type(body.get("usageMetadata")).__name__,
        "known_usage_fields": sorted(allowed.intersection(meta)),
        "other_usage_field_count": len(set(meta) - allowed),
        "usage_token_counts": scalar,
        "counter_observations": counters,
        "alternate_counter_present": any(c["present"] for c in counters[1:]),
        "alternate_counter_positive": any(c["value"] is not None and c["value"] > 0 for c in counters[1:]),
        "observation_reason": reason,
        "response_header_fingerprints": header_hashes,
    }
