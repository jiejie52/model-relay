"""Provider observations retain unknown counters and the protocol's native evidence."""
from __future__ import annotations

from typing import Any, Mapping


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def normalize_observed_cache_audit(
    *, protocol: str, response_json: dict[str, Any], response_headers: Mapping[str, str],
    track_facts: dict[str, Any], payload_stripped_bytes: int = 0,
) -> dict[str, Any]:
    headers = {key.lower(): value for key, value in response_headers.items()}
    if protocol == "gemini_native":
        usage = response_json.get("usageMetadata") or response_json.get("usage_metadata") or {}
        prompt = _count(usage.get("promptTokenCount", usage.get("prompt_token_count")))
        completion = _count(usage.get("candidatesTokenCount", usage.get("candidates_token_count")))
        cached = _count(usage.get("cachedContentTokenCount", usage.get("cached_content_token_count")))
        details = usage.get("cacheTokensDetails", usage.get("cache_tokens_details"))
    elif protocol == "claude_messages":
        usage = response_json.get("usage") or {}
        uncached = _count(usage.get("input_tokens"))
        cached = _count(usage.get("cache_read_input_tokens"))
        written = _count(usage.get("cache_creation_input_tokens"))
        # Claude input_tokens excludes both cache reads and writes. Do not
        # divide cached tokens by that smaller, uncached-only denominator.
        prompt = sum((uncached, cached, written)) if None not in (uncached, cached, written) else None
        completion = _count(usage.get("output_tokens"))
        details = {key: usage[key] for key in (
            "cache_read_input_tokens", "cache_creation_input_tokens", "cache_creation",
        ) if key in usage}
    else:
        usage = response_json.get("usage") or {}
        prompt = _count(usage.get("prompt_tokens", usage.get("input_tokens")))
        completion = _count(usage.get("completion_tokens", usage.get("output_tokens")))
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        details = details if isinstance(details, dict) else {}
        cached = _count(details.get("cached_tokens"))
    return {
        "protocol_profile": protocol,
        "effective_track_a": track_facts.get("controlled") or "none",
        "effective_track_b": "implicit_prefix" if track_facts.get("implicit_effective") else "none",
        "prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached,
        "cache_hit_ratio": round(cached / prompt, 4) if prompt and cached is not None else None,
        "cache_tokens_details": details,
        "payload_stripped_bytes": max(0, payload_stripped_bytes),
        "x_aihubmix_request_id": headers.get("x-aihubmix-request-id"),
        "x_aihubmix_router_sticky": headers.get("x-aihubmix-router-sticky"),
        "x_aihubmix_router_resolved_model": headers.get("x-aihubmix-router-resolved-model"),
        "x_aihubmix_router_fallback": headers.get("x-aihubmix-router-fallback"),
        **({"uncached_input_tokens": uncached, "cache_write_tokens": written}
           if protocol == "claude_messages" else {}),
    }
