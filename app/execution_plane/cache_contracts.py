from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from app.core.idempotency import stable_hash
from app.control_plane.cache_contracts import CacheMode, CacheMechanism, CacheResolutionStatus

@dataclass(frozen=True)
class CacheConditionSet:
    """Transient eligibility facts, before constructing the single Request plan."""
    common: dict[str, Any]
    eligible: tuple[dict[str, Any], ...]
    rejections: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CacheIntentPlan:
    requested_mode: CacheMode
    planned_mechanism: CacheMechanism | None
    resolution_status: CacheResolutionStatus
    decision_reason: str
    context_plan_hash: str
    policy_hash: str | None
    profile_hash: str | None
    cache_contract_hash: str | None
    scope: str
    requested_mechanism: str = "auto"
    reuse_prediction: dict[str, Any] = field(default_factory=dict)
    selection_evidence: dict[str, Any] = field(default_factory=dict)
    allow_uncached_same_context: bool = False
    final_threshold_guard: bool = False
    candidate_rejections: tuple[dict[str, Any], ...] = ()
    mechanism_config: dict[str, Any] = field(default_factory=dict)
    # A fallback is part of the frozen Request plan, not a runtime invention.
    # Only mechanisms certified or explicitly admitted for Auto trial by the
    # frozen Supply and Profile may appear here. CacheOrchestrator may resolve one before dispatch is sealed.
    fallback_mechanisms: tuple[dict[str, Any], ...] = ()
    resolver_version: str = "relay-cache-resolver/3"

    def canonical(self) -> dict[str, Any]:
        return {
            "schema_version": "relay-cache-plan/1",
            "requested_mode": self.requested_mode,
            "requested_mechanism": self.requested_mechanism,
            "reuse_prediction": self.reuse_prediction,
            "selection_evidence": self.selection_evidence,
            "planned_mechanism": self.planned_mechanism,
            "resolution_status": self.resolution_status,
            "decision_reason": self.decision_reason,
            "context_plan_hash": self.context_plan_hash,
            "policy_hash": self.policy_hash,
            "profile_hash": self.profile_hash,
            "cache_contract_hash": self.cache_contract_hash,
            "scope": self.scope,
            "allow_uncached_same_context": self.allow_uncached_same_context,
            "final_threshold_guard": self.final_threshold_guard,
            "candidate_rejections": list(self.candidate_rejections),
            "mechanism_config": self.mechanism_config,
            "fallback_mechanisms": list(self.fallback_mechanisms),
            "resolver_version": self.resolver_version,
        }

    @property
    def plan_hash(self) -> str:
        return stable_hash(self.canonical())

