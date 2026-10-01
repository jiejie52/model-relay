from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..core.idempotency import stable_hash

CacheMode = Literal["off", "auto", "on"]
CacheMechanism = Literal["stateful_resource", "breakpoint", "implicit_prefix"]
CacheResolutionStatus = Literal["pending", "finalized"]


class CacheDecisionError(RuntimeError):
    def __init__(self, code: str, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason

    def public_detail(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.reason:
            value["reason"] = self.reason
        return value


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
    allow_uncached_same_context: bool = False
    final_threshold_guard: bool = False
    candidate_rejections: tuple[dict[str, Any], ...] = ()
    mechanism_config: dict[str, Any] = field(default_factory=dict)
    resolver_version: str = "relay-cache-resolver/1"

    def canonical(self) -> dict[str, Any]:
        return {
            "schema_version": "relay-cache-plan/1",
            "requested_mode": self.requested_mode,
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
            "resolver_version": self.resolver_version,
        }

    @property
    def plan_hash(self) -> str:
        return stable_hash(self.canonical())


@dataclass(frozen=True)
class CacheExecutionBinding:
    mechanism: CacheMechanism | None
    decision_reason: str
    context_plan_hash: str
    plan_hash: str
    binding_version: int = 1
    resource_id: str | None = None
    resource_generation: int | None = None
    provider_handle: str | None = None
    expires_at: str | None = None
    breakpoint_anchors: tuple[dict[str, Any], ...] = ()
    prefix: dict[str, Any] = field(default_factory=dict)
    material_binding_hash: str | None = None
    projection_version: str = "relay-cache-projection/1"
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical(self, *, include_provider_handle: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": "relay-cache-binding/1",
            "mechanism": self.mechanism,
            "decision_reason": self.decision_reason,
            "context_plan_hash": self.context_plan_hash,
            "plan_hash": self.plan_hash,
            "binding_version": self.binding_version,
            "resource_id": self.resource_id,
            "resource_generation": self.resource_generation,
            "expires_at": self.expires_at,
            "breakpoint_anchors": list(self.breakpoint_anchors),
            "prefix": self.prefix,
            "material_binding_hash": self.material_binding_hash,
            "projection_version": self.projection_version,
            "metadata": self.metadata,
        }
        if include_provider_handle:
            value["provider_handle"] = self.provider_handle
        return value

    @property
    def binding_hash(self) -> str:
        return stable_hash(self.canonical(include_provider_handle=True))

    def public_summary(self) -> dict[str, Any]:
        return {
            "execution_mechanism": self.mechanism,
            "decision_reason": self.decision_reason,
        }


@dataclass(frozen=True)
class ExecutionFence:
    owner: str
    epoch: int
    deadline: str | None = None
    kind: Literal["async_job", "sync_request"] = "async_job"

    def canonical(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "epoch": self.epoch,
            "deadline": self.deadline,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class CacheUsageObservation:
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    uncached_input_tokens: int | None
    actual_cache_hit_status: str
    evidence_level: str
    attribution: str = "provider_reported"
    transparent_observation: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "actual_cache_hit_status": self.actual_cache_hit_status,
            "evidence_level": self.evidence_level,
            "attribution": self.attribution,
            "transparent_observation": self.transparent_observation,
            "raw": self.raw,
        }
