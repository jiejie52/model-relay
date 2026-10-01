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
) -> CacheUsageObservation:
    raw = dict(usage or {})
    protocol_n = str(protocol or "").lower()
    read: int | None = None
    write: int | None = None
    uncached: int | None = None
    evidence = "unknown"

    if protocol_n == "claude_messages":
        read = _int_or_none(raw.get("cache_read_input_tokens"))
        write = _int_or_none(raw.get("cache_creation_input_tokens"))
        evidence = "provider_usage" if read is not None or write is not None else "unknown"
    elif protocol_n == "gemini_native":
        gemini_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else raw
        read = _int_or_none(gemini_raw.get("cachedContentTokenCount"))
        evidence = "provider_usage" if read is not None else "unknown"
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

    transparent = effective_mechanism is None and read is not None and read > 0
    return CacheUsageObservation(
        cache_read_tokens=read,
        cache_write_tokens=write,
        uncached_input_tokens=uncached,
        actual_cache_hit_status=status,
        evidence_level=evidence,
        transparent_observation=transparent,
        raw=raw,
    )
