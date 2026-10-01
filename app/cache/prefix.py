from __future__ import annotations

from typing import Any

from ..core.idempotency import stable_hash


def prepare_implicit_prefix_binding(
    *,
    plan: dict[str, Any],
    context_plan: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    cfg = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
    key_source = str(cfg.get("key_source") or "derived")
    hint: str | None = None
    if key_source == "legacy_session_prompt_cache_key":
        hint = str(session.get("prompt_cache_key") or "") or None
    elif bool(cfg.get("send_prompt_cache_key", False)):
        # A deterministic, non-content-bearing prefix key. The actual content
        # fingerprint remains separate and must still match.
        hint = "relay-cache-" + stable_hash(
            {
                "session_id": str(session.get("id") or ""),
                "prefix": context_plan.get("stable_prefix_fingerprint"),
            }
        )[:40]
    return {
        "prefix_fingerprint": context_plan.get("stable_prefix_fingerprint"),
        "prompt_cache_key": hint,
        "retention": cfg.get("retention"),
    }
