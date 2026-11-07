"""Freeze one eligibility-based plan; retain historical plan formats for recovery."""
from __future__ import annotations

from typing import Any

from .contracts import CacheConditionSet, CacheDecisionError, CacheIntentPlan
from ..core.idempotency import stable_hash


def freeze_dual_track_plan(intent: CacheIntentPlan | CacheConditionSet, *, snapshot: dict[str, Any],
                           protocol_profile: dict[str, Any], contract: dict[str, Any],
                           cache_policy: dict[str, Any]) -> dict[str, Any]:
    if (int((snapshot.get("cache_upgrade") or {}).get("version") or 0) >= 4
            and (snapshot.get("cache_upgrade") or {}).get("grok_mode") != "legacy_v1"):
        return build_unified_plan(intent, protocol_profile=protocol_profile, contract=contract)
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
    if int((snapshot.get("cache_upgrade") or {}).get("version") or 0) >= 3:
        from .intent_resolver import CacheIntentResolver
        from .contracts import CacheDecisionError

        # Each track has a replayable plan, not merely a flag derived from the
        # selected primary mechanism. No resources or Provider calls occur here.
        implicit_plan = None
        rejection = "not_authorized"
        if base and permission:
            try:
                candidate = CacheIntentResolver().resolve(
                    requested_mode=intent.requested_mode, requested_mechanism="implicit_prefix",
                    context_plan=snapshot["context_plan"] if "context_plan" in snapshot else
                        {"context_plan_hash": intent.context_plan_hash,
                         "stable_prefix_segment_ids": snapshot.get("stable_prefix_segment_ids") or [],
                         "cache_upgrade": snapshot.get("cache_upgrade") or {}},
                    cache_contract=contract, protocol_profile=protocol_profile, cache_policy=cache_policy,
                    cache_contract_hash=intent.cache_contract_hash, profile_hash=intent.profile_hash,
                    policy_hash=intent.policy_hash, provider=snapshot.get("provider"), model=snapshot.get("model"),
                )
                if candidate.planned_mechanism == "implicit_prefix":
                    implicit_plan = candidate.canonical()
                rejection = candidate.decision_reason
            except CacheDecisionError as exc:
                rejection = exc.code
        plan["tracks"]["implicit"].update(
            provider_admitted=(implicit_plan is not None or (
                selected == "breakpoint" and compatibility == "composite_layered")),
            plan=implicit_plan, rejection_reason=None if implicit_plan else rejection,
            layered_authorized=(selected == "breakpoint" and compatibility == "composite_layered"),
        )
        plan["tracks"]["controlled"]["plan"] = intent.canonical() if controlled else None
        # A fully frozen alternative is the sole authority for pre-dispatch
        # fallback. Forced stateful preference never grants an implicit branch.
        if implicit_plan and controlled == "stateful_resource" and intent.requested_mechanism == "auto":
            plan["tracks"]["implicit"]["fallback_authorized"] = True
    plan["plan_hash"] = stable_hash(plan)
    return plan


UNIFIED_PLAN_VERSION = "relay-unified-cache-plan/1"
SAFE_PREPARATION_REASONS = frozenset({
    "below_minimum", "insufficient_prefix_length", "no_cacheable_prefix", "no_cacheable_contents",
    "physical_layout_unavailable", "tail_turn_reservation_violation", "history_material_projection_unsafe",
    "cached_content_unsupported_provider_fields", "serialized_request_too_large",
    "explicit_not_economical_vs_implicit", "current_read_and_future_bonus_insufficient",
    "implicit_hit_rate_unverified", "current_read_price_or_creation_fee_unverified",
    "new_resource_cost_measurement_unavailable", "measurement_unavailable", "new_resource_budget_exceeded",
    "hard_budget_price_or_storage_evidence_unverified", "relative_price_policy_invalid",
    "resource_measurement_invalid", "explicit_not_economical_implicit_fallback_unavailable",
})
ASSESSMENT_FAILURE_REASONS = frozenset({
    "prefix_assessment_unknown", "implicit_prefix_threshold_unverified",
    "implicit_prefix_measurement_unavailable", "implicit_prefix_measurement_failed",
    "cache_measurement_failed", "stateful_adapter_unavailable",
})


def build_unified_plan(conditions: CacheConditionSet | CacheIntentPlan, *,
                       protocol_profile: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Build one complete plan only after the common eligibility screen."""
    if isinstance(conditions, CacheIntentPlan):
        if conditions.planned_mechanism is not None:
            raise CacheDecisionError("CACHE_CONDITIONS_REQUIRED", "Unified planning requires eligibility facts before mechanism selection")
        # Off / no eligible prefix already has a finalized no-cache result.
        common, eligible, rejections = conditions.canonical(), (), conditions.candidate_rejections
    else:
        common, eligible, rejections = conditions.common, conditions.eligible, conditions.rejections
    plan = dict(common)
    mode, preference = plan["requested_mode"], plan["requested_mechanism"]
    allowed = {}
    rejected = list(rejections)
    controlled = None
    for candidate in eligible:
        name = candidate["mechanism"]
        if (preference == "implicit_prefix" or mode == "on" and preference != "auto") and name != preference:
            rejected.append({"mechanism": name, "reason": "caller_mechanism_constraint"})
            continue
        if name in {"stateful_resource", "breakpoint"}:
            if controlled is not None:
                rejected.append({"mechanism": name, "reason": "controlled_track_preference"})
                continue
            controlled = name
        allowed[name] = candidate
    implicit = "implicit_prefix" in allowed
    wire = protocol_profile.get("cache") or {}
    compatibility = (
        "mutually_exclusive" if protocol_profile.get("protocol") == "gemini_native"
        else "composite_layered" if wire.get("composite_mode") == contract.get("composite_mode") == "composite_layered"
        and controlled == "breakpoint" else "exclusive_by_policy"
    )
    if compatibility == "composite_layered" and implicit:
        outer = allowed["implicit_prefix"]["coverage_segment_ids"]
        inner = allowed[controlled]["coverage_segment_ids"]
        if inner != outer[:len(inner)]:
            raise CacheDecisionError("CACHE_PREFIX_INCLUSION_INVALID", "Controlled prefix is not contained in the implicit prefix")
    preferred = next(iter(allowed), None)
    plan.update({
        "schema_version": UNIFIED_PLAN_VERSION,
        "planned_mechanism": preferred,
        "resolution_status": "pending" if allowed else "finalized",
        "decision_reason": "conditions_accepted_pending_arbitration" if allowed else plan.get("decision_reason", "no_eligible_track"),
        "candidates": allowed,
        "candidate_rejections": rejected,
        "mechanism_config": (allowed.get(preferred) or {}).get("mechanism_config", {}),
        "final_threshold_guard": (allowed.get(preferred) or {}).get("final_threshold_guard", False),
        "fallback_mechanisms": [],
        "tracks": {
            "implicit": {"plan_enabled": implicit, "provider_admitted": implicit, "legacy_allowed": False},
            "controlled": {"candidate": controlled, "selected": controlled if preferred == controlled else None},
            "compatibility": compatibility,
        },
        "arbitration": {
            "version": "relay-cache-arbitration/1", "candidate_order": list(allowed),
            "compatibility": compatibility,
            "pending_checks": ["material_projection", "token_threshold", "resource_validity", "creation_cost"] if allowed else [],
            "allow_none": bool(plan.get("allow_uncached_same_context")) or not allowed,
            "allowed_fallback_reasons": sorted(SAFE_PREPARATION_REASONS),
            "assessment_failure_reasons": sorted(ASSESSMENT_FAILURE_REASONS),
        },
    })
    plan["plan_hash"] = stable_hash(plan)
    return plan


def candidate_execution_plan(plan: dict[str, Any], mechanism: str) -> dict[str, Any]:
    """A runtime view of an authorized branch; identity remains the unified hash."""
    candidate = (plan.get("candidates") or {}).get(mechanism)
    if not isinstance(candidate, dict):
        raise CacheDecisionError("CACHE_BRANCH_NOT_AUTHORIZED", "Mechanism is absent from the frozen unified plan")
    return {**plan, **candidate, "planned_mechanism": mechanism}


def arbitrate_unified_plan(plan: dict[str, Any], assessments: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Select only frozen branches, after read-only assessments and before create."""
    order = (plan.get("arbitration") or {}).get("candidate_order") or []
    if any(name not in (plan.get("candidates") or {}) for name in assessments):
        raise CacheDecisionError("CACHE_BRANCH_NOT_AUTHORIZED", "Runtime assessment introduced an unplanned mechanism")
    ready = []
    for name in order:
        fact = assessments.get(name, {})
        if fact.get("eligible") is not True:
            continue
        if name == "stateful_resource" and fact.get("resource_action") == "would_create":
            cost_allow = fact.get("cost", {}).get("allow")
            if cost_allow is False or plan.get("requested_mode") == "auto" and cost_allow is not True:
                continue
        ready.append(name)
    compatibility = (plan.get("tracks") or {}).get("compatibility")
    if compatibility == "composite_layered" and all(name in ready for name in ("breakpoint", "implicit_prefix")):
        selected = ["breakpoint", "implicit_prefix"]
        reason = "certified_layered_tracks"
    elif compatibility == "mutually_exclusive" and all(name in ready for name in ("stateful_resource", "implicit_prefix")):
        explicit = assessments["stateful_resource"]
        if explicit.get("resource_action") == "reuse":
            selected, reason = ["stateful_resource"], "existing_compatible_resource_preferred"
        elif explicit.get("cost", {}).get("allow") is True:
            selected, reason = ["stateful_resource"], "explicit_cost_lower_than_implicit"
        else:
            selected, reason = ["implicit_prefix"], "implicit_preferred_without_positive_create_evidence"
    else:
        selected = ready[:1]
        reason = "eligible_track_selected" if selected else next(
            (assessments[name].get("reason") for name in order if assessments.get(name, {}).get("eligible") is False),
            "no_economical_runtime_track" if assessments else "no_eligible_runtime_track",
        )
        if compatibility == "mutually_exclusive" and selected:
            reason = "mutually_exclusive_cost_and_eligibility"
    if not selected and plan.get("requested_mode") == "on" and any(
        fact.get("resource_action") == "would_create" and fact.get("cost", {}).get("allow") is False
        for fact in assessments.values()
    ):
        raise CacheDecisionError("CACHE_NEW_RESOURCE_BUDGET_DENIED", "No authorized cache branch satisfies the frozen creation budget")
    failures = (plan.get("arbitration") or {}).get("assessment_failure_reasons") or []
    if not selected and any(fact.get("reason") in failures for fact in assessments.values()) and (
        plan.get("requested_mode") == "on" or not (plan.get("arbitration") or {}).get("allow_none")
    ):
        raise CacheDecisionError("CACHE_ASSESSMENT_FAILED", "No authorized branch has verified runtime eligibility")
    return {"version": "relay-cache-arbitration/1", "selected_mechanisms": selected,
            "reason": reason, "compatibility": compatibility, "assessments": assessments}


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
