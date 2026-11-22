from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.core.idempotency import stable_hash
from app.shared_contracts import ContinuationDecisionError

ContinuationMode = Literal["off", "auto", "prefer_stateful_anchor", "require_stateful_anchor"]


@dataclass(frozen=True)
class ContinuationIntentPlan:
    continuation_mode: str = "off"
    resolution_status: str = "disabled"
    decision_reason: str = "continuation_not_enabled"
    planned_anchor_class: str | None = None
    requested_mode: str = "off"
    parent_anchor_id: str | None = None
    parent_history_version: int | None = None
    parent_fingerprint: str | None = None
    allow_fallback_to_full: bool = True
    schema_version: str = "relay-continuation-plan/2"
    plan_hash: str = ""
    topology: dict[str, Any] | None = None

    def __post_init__(self):
        if not self.plan_hash:
            object.__setattr__(self, "plan_hash", stable_hash(self.canonical(include_hash=False)))

    def canonical(self, *, include_hash: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "continuation_mode": self.continuation_mode,
            "resolution_status": self.resolution_status,
            "decision_reason": self.decision_reason,
            "planned_anchor_class": self.planned_anchor_class,
            "requested_mode": self.requested_mode,
            "parent_anchor_id": self.parent_anchor_id,
            "parent_history_version": self.parent_history_version,
            "parent_fingerprint": self.parent_fingerprint,
            "allow_fallback_to_full": self.allow_fallback_to_full,
        }
        if self.topology is not None:
            data["topology"] = self.topology
        if include_hash:
            data["plan_hash"] = self.plan_hash or stable_hash(data)
        return data

    def to_dict(self) -> dict[str, Any]:
        return self.canonical(include_hash=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContinuationIntentPlan:
        schema_version = str(data.get("schema_version") or "relay-continuation-plan/1")
        if schema_version == "relay-continuation-plan/1":
            continuation_mode = str(data.get("continuation_mode") or "auto")
            is_off = continuation_mode.strip().lower() in {"off", "none", "disabled", "false"}
            resolution_status = str(data.get("resolution_status") or "disabled")
            decision_reason = str(
                data.get("decision_reason")
                or ("continuation_mode_off" if is_off else "continuation_not_enabled")
            )
            requested_mode = str(data.get("requested_mode") or ("off" if is_off else continuation_mode))
        else:
            continuation_mode = str(data.get("continuation_mode") or "off")
            resolution_status = str(data.get("resolution_status") or "disabled")
            decision_reason = str(data.get("decision_reason") or "continuation_not_enabled")
            requested_mode = str(data.get("requested_mode") or "off")
        return cls(
            continuation_mode=continuation_mode,
            resolution_status=resolution_status,
            decision_reason=decision_reason,
            planned_anchor_class=data.get("planned_anchor_class"),
            requested_mode=requested_mode,
            parent_anchor_id=data.get("parent_anchor_id"),
            parent_history_version=data.get("parent_history_version"),
            parent_fingerprint=data.get("parent_fingerprint"),
            allow_fallback_to_full=bool(data.get("allow_fallback_to_full", True)),
            schema_version=schema_version,
            plan_hash=str(data.get("plan_hash") or ""),
            topology=data.get("topology"),
        )
