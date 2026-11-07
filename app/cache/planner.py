"""Persist independent implicit and controlled candidates alongside the existing cache plan."""
from __future__ import annotations

from typing import Any

from .contracts import CacheIntentPlan
from ..core.idempotency import stable_hash


def freeze_dual_track_plan(intent: CacheIntentPlan, *, snapshot: dict[str, Any],
                           protocol_profile: dict[str, Any], contract: dict[str, Any],
                           cache_policy: dict[str, Any]) -> dict[str, Any]:
    plan = intent.canonical()
    profile = protocol_profile.get("cache") or {}
    selected = intent.planned_mechanism
    protocol = protocol_profile.get("protocol")
    controlled = selected if selected in {"stateful_resource", "breakpoint"} else None
    controlled_candidate = controlled or intent.selection_evidence.get("controlled_candidate")
    profile_mode = profile.get("composite_mode")
    compatibility = (
        "mutually_exclusive" if protocol == "gemini_native"
        else "composite_layered" if profile_mode == contract.get("composite_mode") == "composite_layered"
        else "implicit_only"
    )
    implicit_admitted = selected == "implicit_prefix" or any(
        candidate.get("mechanism") == "implicit_prefix" for candidate in intent.fallback_mechanisms
    )
    permission = intent.requested_mode != "off" and not cache_policy.get("deny") and not (
        intent.requested_mode == "auto" and cache_policy.get("auto_disabled")
    )
    base = permission and (snapshot.get("cache_upgrade") or {}).get("grok_mode") != "legacy_v1"
    if intent.requested_mode == "on" and intent.requested_mechanism == "stateful_resource":
        base = False
    plan["tracks"] = {
        "implicit": {
            "plan_enabled": bool(base),
            "legacy_allowed": bool(permission and selected == "implicit_prefix"),
            "provider_admitted": implicit_admitted or (selected == "breakpoint" and compatibility == "composite_layered"),
        },
        "controlled": {"candidate": controlled_candidate, "selected": controlled},
        "compatibility": compatibility,
    }
    if (snapshot.get("cache_upgrade") or {}).get("version") == 2:
        plan["implicit_prefix_plan"] = (snapshot.get("context_plan") or {}).get("implicit_prefix_plan")
    plan["plan_hash"] = stable_hash(plan)
    return plan


def final_track_facts(plan: dict[str, Any], mechanism: str | None) -> dict[str, bool | str | None]:
    tracks = plan.get("tracks") or {}
    implicit = tracks.get("implicit") or {}
    layered = tracks.get("compatibility") == "composite_layered"
    return {
        "implicit_base": bool(implicit.get("plan_enabled")) and mechanism != "stateful_resource",
        "implicit_effective": bool(implicit.get("plan_enabled")) and (
            mechanism == "implicit_prefix"
            or (mechanism == "breakpoint" and layered and implicit.get("provider_admitted") is True)
        ),
        "controlled": mechanism if mechanism in {"stateful_resource", "breakpoint"} else None,
    }
