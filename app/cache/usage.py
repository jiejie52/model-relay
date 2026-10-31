from __future__ import annotations

from typing import Any

from .contracts import CacheUsageObservation


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def normalize_cache_usage(
    *,
    protocol: str | None,
    usage: dict[str, Any] | None,
    effective_mechanism: str | None,
    requested_mode: str,
    effective_mechanisms: list[str] | None = None,
    binding_records: list[dict[str, Any]] | None = None,
) -> CacheUsageObservation:
    raw = dict(usage or {})
    protocol_n = str(protocol or "").lower()
    read: int | None = None
    write: int | None = None
    uncached: int | None = None
    prompt_total: int | None = None
    cache_read_ratio: float | None = None
    evidence = "unknown"

    if protocol_n == "claude_messages":
        read = _int_or_none(raw.get("cache_read_input_tokens"))
        write = _int_or_none(raw.get("cache_creation_input_tokens"))
        evidence = "provider_usage" if read is not None or write is not None else "unknown"
    elif protocol_n == "gemini_native":
        gemini_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else raw
        read = _int_or_none(gemini_raw.get("cachedContentTokenCount"))
        prompt_total = _int_or_none(gemini_raw.get("promptTokenCount"))
        if prompt_total is None:
            prompt_total = _int_or_none(raw.get("prompt_tokens"))
        if prompt_total is not None and read is not None:
            # Gemini promptTokenCount includes cached content. Preserve the
            # provider total and derive the uncached portion separately.
            uncached = max(0, prompt_total - read)
            cache_read_ratio = (read / prompt_total) if prompt_total > 0 else None
        evidence = "provider_usage" if read is not None or prompt_total is not None else "unknown"
    elif protocol_n in {"responses", "chat_completions", "moonshot_chat"}:
        input_details = raw.get("input_tokens_details") if isinstance(raw.get("input_tokens_details"), dict) else {}
        prompt_details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
        candidates = [
            input_details.get("cached_tokens"),
            prompt_details.get("cached_tokens"),
            raw.get("cached_tokens"),
            raw.get("prompt_cache_hit_tokens"),
        ]
        for item in candidates:
            value = _int_or_none(item)
            if value is not None:
                read = value
                break
        miss = _int_or_none(raw.get("prompt_cache_miss_tokens"))
        if miss is not None:
            uncached = miss
        evidence = "provider_usage" if read is not None or miss is not None else "unknown"

    if read is not None and read > 0:
        status = "hit"
    elif write is not None and write > 0:
        status = "write_only"
    elif read == 0 and effective_mechanism is not None:
        status = "miss"
    elif effective_mechanism is None and requested_mode == "off" and read in {None, 0}:
        status = "not_requested"
    else:
        status = "unknown"

    mechanisms = [str(value) for value in (effective_mechanisms or []) if str(value)]
    if not mechanisms and effective_mechanism:
        mechanisms = [str(effective_mechanism)]
    records = [dict(item) for item in (binding_records or []) if isinstance(item, dict)]
    binding_observations: list[dict[str, Any]] = []
    raw_binding_usage = raw.get("cache_binding_usage") if isinstance(raw.get("cache_binding_usage"), dict) else {}
    for item in records:
        binding_id = str(item.get("binding_id") or "")
        specific = raw_binding_usage.get(binding_id) if isinstance(raw_binding_usage.get(binding_id), dict) else {}
        specific_read = _int_or_none(specific.get("cache_read_tokens"))
        specific_write = _int_or_none(specific.get("cache_write_tokens"))
        reported_specific_status = str(specific.get("actual_cache_hit_status") or specific.get("status") or "").lower()
        if reported_specific_status in {"hit", "miss", "write_only", "unknown"}:
            specific_status = reported_specific_status
        elif specific_read is not None and specific_read > 0:
            specific_status = "hit"
        elif specific_write is not None and specific_write > 0:
            specific_status = "write_only"
        elif specific_read == 0:
            specific_status = "miss"
        elif len(mechanisms) == 1 and sum(
            1 for record in records if record.get("active", record.get("mechanism") is not None)
        ) == 1 and bool(item.get("active", item.get("mechanism") is not None)):
            specific_status = status
        else:
            specific_status = "unknown"
        binding_observations.append({
            "binding_id": binding_id,
            "role": item.get("role"),
            "mechanism": item.get("mechanism"),
            "state": item.get("state"),
            "resource_action": item.get("resource_action"),
            "projection_hash": item.get("projection_hash"),
            "resource_generation": item.get("resource_generation"),
            "attached": bool(item.get("active", item.get("mechanism") is not None)),
            "actual_cache_hit_status": specific_status,
            "cache_read_tokens": specific_read,
            "cache_write_tokens": specific_write,
            "evidence_level": "provider_binding_usage" if specific else (
                "provider_usage_single_binding" if len(mechanisms) == 1 and sum(
                    1 for record in records if record.get("active", record.get("mechanism") is not None)
                ) == 1 and bool(item.get("active", item.get("mechanism") is not None)) else "aggregate_unattributed"
            ),
        })

    transparent = not mechanisms and read is not None and read > 0
    return CacheUsageObservation(
        cache_read_tokens=read,
        cache_write_tokens=write,
        uncached_input_tokens=uncached,
        actual_cache_hit_status=status,
        evidence_level=evidence,
        transparent_observation=transparent,
        raw=raw,
        prompt_tokens_total=prompt_total,
        cache_read_ratio=cache_read_ratio,
        binding_observations=tuple(binding_observations),
    )
