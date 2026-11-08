from __future__ import annotations
from copy import deepcopy
from typing import Any
from dataclasses import replace
from app.core.idempotency import stable_hash
from app.shared_contracts import CacheDecisionError
from app.runtime.cache_contracts import CacheExecutionBinding
from app.runtime.breakpoints import prepare_breakpoint_binding

UNIFIED_PLAN_VERSION = "relay-unified-cache-plan/1"
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


class CacheRuntimeArbiter:
    """Read-only inventory/measurement followed by one centralized arbitration."""
    def __init__(self, stateful, prepare_implicit):
        self.stateful, self.prepare_implicit = stateful, prepare_implicit

    async def evaluate_and_arbitrate(self, *, request_row, snapshot, session, context_plan, material_bindings,
                                     fence, history, provider_physical_plan):
        plan = snapshot["cache_plan"]
        plan_hash = str(snapshot.get("cache_plan_hash") or "")
        if (plan_hash != plan.get("plan_hash")
                or plan_hash != stable_hash({k: v for k, v in plan.items() if k != "plan_hash"})
                or plan.get("context_plan_hash") != context_plan.get("context_plan_hash")):
            raise CacheDecisionError("CACHE_PLAN_INTEGRITY_MISMATCH", "Unified cache plan does not match the accepted snapshot")
        candidates = plan.get("candidates") or {}
        material_hash = stable_hash(material_bindings)
        assessments = {}
        implicit_binding = None
        controlled_result = None
        controlled_plan = None
        controlled = (plan.get("tracks") or {}).get("controlled", {}).get("candidate")
        safe_reasons = (plan.get("arbitration") or {}).get("allowed_fallback_reasons") or []
        assessment_failures = (plan.get("arbitration") or {}).get("assessment_failure_reasons") or []
        common = {"request_row": request_row, "snapshot": snapshot, "session": session,
                  "context_plan": context_plan, "material_bindings": material_bindings,
                  "fence": fence, "history": history, "provider_physical_plan": provider_physical_plan}

        # These are read-only Provider assessments. No track creates a cache
        # resource before centralized arbitration has selected its branch.
        if "implicit_prefix" in candidates:
            implicit_plan = candidate_execution_plan(plan, "implicit_prefix")
            try:
                implicit_binding = await self.prepare_implicit(
                    plan=implicit_plan, plan_hash=plan_hash, snapshot=snapshot, session=session,
                    context_plan=context_plan, material_binding_hash=material_hash,
                    provider_physical_plan=provider_physical_plan, history=history,
                )
            except CacheDecisionError as exc:
                if exc.reason not in assessment_failures:
                    raise
                implicit_binding = CacheExecutionBinding(
                    mechanism=None, decision_reason=exc.reason, context_plan_hash=context_plan["context_plan_hash"],
                    plan_hash=plan_hash, material_binding_hash=material_hash, metadata={"assessment_error": exc.code},
                )
            assessments["implicit_prefix"] = {
                "eligible": implicit_binding.mechanism == "implicit_prefix",
                "reason": implicit_binding.decision_reason,
                "threshold": dict(implicit_binding.metadata),
            }
        if controlled == "stateful_resource":
            controlled_plan = deepcopy(candidate_execution_plan(plan, controlled))
            if not assessments.get("implicit_prefix", {}).get("eligible"):
                economics = controlled_plan["mechanism_config"].get("auto_economics") or {}
                economics.update(certified_implicit_alternative=False, trial_implicit_alternative=False)
            try:
                controlled_result = await self.stateful.prepare(plan=controlled_plan, assessment_only=True, **common)
            except CacheDecisionError as exc:
                if exc.reason not in assessment_failures:
                    raise
                controlled_result = {"mechanism": None, "decision_reason": exc.reason,
                                     "metadata": {"assessment_error": exc.code}}
            reason = str(controlled_result.get("decision_reason") or "unknown")
            if controlled_result.get("mechanism") is None and reason not in safe_reasons and reason not in assessment_failures:
                raise CacheDecisionError("CACHE_ASSESSMENT_FAILED", "Resource assessment did not establish a safe branch", reason=reason)
            meta = controlled_result.get("metadata") or {}
            assessments[controlled] = {"eligible": controlled_result.get("mechanism") == controlled,
                                       "reason": reason, "resource_action": (meta.get("cache_resource_action")
                                           or ("reuse" if reason.startswith("resource_reused") else None)),
                                       "cost": meta.get("cache_cost_decision") or {},
                                       "prefix_token_count": meta.get("prefix_token_count")}
        elif controlled == "breakpoint":
            controlled_plan = candidate_execution_plan(plan, controlled)
            anchors = prepare_breakpoint_binding(plan=controlled_plan, context_plan=context_plan)
            controlled_result = CacheExecutionBinding(
                mechanism=controlled if anchors else None,
                decision_reason="selected" if anchors else "no_cacheable_prefix",
                context_plan_hash=context_plan["context_plan_hash"], plan_hash=plan_hash,
                material_binding_hash=material_hash, breakpoint_anchors=anchors,
            )
            assessments[controlled] = {"eligible": bool(anchors), "reason": controlled_result.decision_reason}

        decision = arbitrate_unified_plan(plan, assessments)
        return {"plan": plan, "plan_hash": plan_hash, "candidates": candidates, "material_hash": material_hash,
                "assessments": assessments, "implicit_binding": implicit_binding, "controlled_result": controlled_result,
                "controlled_plan": controlled_plan, "controlled": controlled, "safe_reasons": safe_reasons,
                "assessment_failures": assessment_failures, "common": common, "decision": decision}
