from __future__ import annotations

from typing import Any

from ..core.idempotency import stable_hash
from .contracts import CacheDecisionError


def prepare_implicit_prefix_binding(
    *,
    plan: dict[str, Any],
    context_plan: dict[str, Any],
    session: dict[str, Any],
    provider: str | None = None,
    snapshot: dict[str, Any] | None = None,
    provider_physical_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
    if str(provider or "").strip().lower() == "gemini":
        physical = provider_physical_plan if isinstance(provider_physical_plan, dict) else {}
        if (
            str(physical.get("layout_version") or "") != "gemini-physical-cache-layout/9"
            or not bool(physical.get("implicit_prefix_eligible"))
            or not str(physical.get("implicit_prefix_fingerprint") or "")
            or not isinstance(physical.get("implicit_prefix_payload"), dict)
        ):
            raise CacheDecisionError(
                "GEMINI_IMPLICIT_PREFIX_LAYOUT_UNSUPPORTED",
                "Gemini implicit-prefix execution requires a validated layout/9 physical projection",
                reason="physical_layout_unavailable",
            )
        dependencies = physical.get("dependencies") if isinstance(physical.get("dependencies"), dict) else {}
        return {
            "schema_version": "relay-gemini-implicit-prefix-binding/1",
            "prefix_fingerprint": str(physical.get("implicit_prefix_fingerprint")),
            "prefix_version": int(dependencies.get("safe_history_prefix_version") or 0),
            "compatible_prefix_fingerprints": dict(
                physical.get("implicit_prefix_compatible_fingerprints") or {}
            ),
            "layout_version": physical.get("layout_version"),
            "projector_version": physical.get("projector_version"),
            "physical_plan_hash": physical.get("physical_plan_hash"),
            "implicit_prefix_payload_hash": physical.get("implicit_prefix_payload_hash"),
            # Gemini implicit caching is provider-managed and has no CachedContent
            # handle or caller-visible prompt_cache_key on this native API path.
            "prompt_cache_key": None,
            "retention": cfg.get("retention"),
        }

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
