from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..core.idempotency import stable_hash

CacheMode = Literal["off", "auto", "on"]
CacheMechanism = Literal["stateful_resource", "breakpoint", "implicit_prefix"]
CacheResolutionStatus = Literal["pending", "finalized"]
CACHE_BINDING_PLAN_VERSION = "relay-cache-binding-plan/2"
CACHE_BINDING_SET_VERSION = "relay-cache-binding-set/2"


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
    requested_mechanism: str = "auto"
    reuse_prediction: dict[str, Any] = field(default_factory=dict)
    selection_evidence: dict[str, Any] = field(default_factory=dict)
    allow_uncached_same_context: bool = False
    final_threshold_guard: bool = False
    candidate_rejections: tuple[dict[str, Any], ...] = ()
    mechanism_config: dict[str, Any] = field(default_factory=dict)

    # A fallback is part of the frozen Request plan, not a runtime invention.
    # Only mechanisms already certified by the frozen Supply and Profile may
    # appear here. CacheOrchestrator may resolve one before dispatch is sealed.

    resolver_version: str = "relay-cache-resolver/3"

    def canonical(self) -> dict[str, Any]:
        primary_binding = None
        if self.planned_mechanism is not None:
            primary_binding = {
                "binding_id": "primary",
                "role": "primary",
                "mechanism": self.planned_mechanism,
                "projection": self.primary_projection,
                "mechanism_config": self.mechanism_config,
                "resolution_status": self.resolution_status,
                "required": self.requested_mode == "on",
                "decision_reason": self.decision_reason,
            }
        return {
            "schema_version": "relay-cache-plan/2",
            "binding_plan_version": CACHE_BINDING_PLAN_VERSION,
            "requested_mode": self.requested_mode,
            "requested_mechanism": self.requested_mechanism,
            "reuse_prediction": self.reuse_prediction,
            "selection_evidence": self.selection_evidence,
            "planned_mechanism": self.planned_mechanism,
            "primary_binding": primary_binding,
            "additional_cache_bindings": list(self.additional_bindings),
            "binding_composition": self.binding_composition,
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
    mechanism_config: dict[str, Any] = field(default_factory=dict)
    binding_id: str = "primary"
    role: str = "primary"
    projection: dict[str, Any] = field(
        default_factory=lambda: {"kind": "full_stable_prefix"}
    )
    projection_hash: str | None = None
    additional_bindings: tuple[dict[str, Any], ...] = ()

    def _primary_record(self, *, include_provider_handle: bool) -> dict[str, Any]:
        reason = str(self.decision_reason or "")
        if self.mechanism == "stateful_resource":
            if reason.startswith("resource_created"):
                resource_action = "created"
            elif reason.startswith("resource_reused"):
                resource_action = "reused"
            elif self.resource_id:
                resource_action = "attached"
            else:
                resource_action = "skipped"
        elif self.mechanism == "implicit_prefix":
            resource_action = "provider_managed"
        elif self.mechanism == "breakpoint":
            resource_action = "attached"
        else:
            resource_action = "none"
        value: dict[str, Any] = {
            "binding_id": self.binding_id,
            "role": self.role,
            "mechanism": self.mechanism,
            "active": self.mechanism is not None,
            "state": str(self.metadata.get("binding_state") or ("attached" if self.mechanism else "skipped")),
            "decision_reason": self.decision_reason,
            "resource_action": resource_action,
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
            "projection": self.projection,
            "projection_hash": self.projection_hash,
            "metadata": self.metadata,
            "mechanism_config": self.mechanism_config,
        }
        if include_provider_handle:
            value["provider_handle"] = self.provider_handle
        return value

    def canonical(self, *, include_provider_handle: bool = True) -> dict[str, Any]:
        primary = self._primary_record(include_provider_handle=include_provider_handle)
        additional = [dict(item) for item in self.additional_bindings]
        all_bindings = [primary, *additional]
        active_bindings = [item for item in all_bindings if item.get("active", item.get("mechanism") is not None)]
        value: dict[str, Any] = {
            "schema_version": "relay-cache-binding/2",
            "binding_set_version": CACHE_BINDING_SET_VERSION,
            # Scalar aliases remain for existing API/read-model consumers. They
            # describe the Primary Cache only, never the complete binding set.
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
            "mechanism_config": self.mechanism_config,
            "primary_binding": primary,
            "additional_bindings": additional,
            "cache_bindings": all_bindings,
            "active_binding_count": len(active_bindings),
            "active_mechanisms": [item.get("mechanism") for item in active_bindings],
        }
        if include_provider_handle:
            value["provider_handle"] = self.provider_handle
        return value

    @property
    def binding_hash(self) -> str:
        return stable_hash(self.canonical(include_provider_handle=True))

    def public_summary(self) -> dict[str, Any]:
        canonical = self.canonical(include_provider_handle=False)
        return {
            "execution_mechanism": self.mechanism,
            "primary_cache_mechanism": self.mechanism,
            "decision_reason": self.decision_reason,
            "cache_bindings": [
                {
                    "binding_id": item.get("binding_id"),
                    "role": item.get("role"),
                    "mechanism": item.get("mechanism"),
                    "active": item.get("active"),
                    "state": item.get("state"),
                    "decision_reason": item.get("decision_reason"),
                    "resource_action": item.get("resource_action"),
                    "projection_hash": item.get("projection_hash"),
                    "resource_generation": item.get("resource_generation"),
                }
                for item in canonical.get("cache_bindings", [])
            ],
        }


def cache_binding_records(
    binding: dict[str, Any] | None,
    *,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """Read a binding set while preserving compatibility with relay-cache-binding/1."""
    if not isinstance(binding, dict):
        return []
    raw = binding.get("cache_bindings")
    if isinstance(raw, list):
        records = [dict(item) for item in raw if isinstance(item, dict)]
    else:
        records = []
        primary = binding.get("primary_binding")
        if isinstance(primary, dict):
            records.append(dict(primary))
        elif binding.get("mechanism") is not None or binding.get("resource_id") is not None:
            records.append({
                "binding_id": "primary",
                "role": "primary",
                "mechanism": binding.get("mechanism"),
                "active": binding.get("mechanism") is not None,
                "resource_id": binding.get("resource_id"),
                "resource_generation": binding.get("resource_generation"),
                "provider_handle": binding.get("provider_handle"),
                "prefix": binding.get("prefix") if isinstance(binding.get("prefix"), dict) else {},
                "metadata": binding.get("metadata") if isinstance(binding.get("metadata"), dict) else {},
            })
        extra = binding.get("additional_bindings")
        if extra is None:
            extra = binding.get("additional_cache_bindings")
        if isinstance(extra, list):
            records.extend(dict(item) for item in extra if isinstance(item, dict))
    if active_only:
        records = [item for item in records if item.get("active", item.get("mechanism") is not None)]
    return records


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
    prompt_tokens_total: int | None = None
    cache_read_ratio: float | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "prompt_tokens_total": self.prompt_tokens_total,
            "cache_read_tokens": self.cache_read_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_read_ratio": self.cache_read_ratio,
            "cache_write_tokens": self.cache_write_tokens,
            "actual_cache_hit_status": self.actual_cache_hit_status,
            "evidence_level": self.evidence_level,
            "attribution": self.attribution,
            "transparent_observation": self.transparent_observation,
            "binding_observations": list(self.binding_observations),
            "raw": self.raw,
        }
