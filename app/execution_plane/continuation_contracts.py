from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from app.core.idempotency import stable_hash

ContinuationMode = Literal["none", "auto", "delta_turn", "stateful_anchor"]


@dataclass(frozen=True)
class ContinuationIntentPlan:
    continuation_mode: str = "auto"
    parent_anchor_id: str | None = None
    parent_history_version: int | None = None
    parent_fingerprint: str | None = None
    allow_fallback_to_full: bool = True
    schema_version: str = "relay-continuation-plan/1"
    plan_hash: str = ""
    topology: dict[str, Any] | None = None

    def __post_init__(self):
        if not self.plan_hash:
            object.__setattr__(self, "plan_hash", stable_hash(self.canonical(include_hash=False)))

    def canonical(self, *, include_hash: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "continuation_mode": self.continuation_mode,
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
        return cls(
            continuation_mode=data.get("continuation_mode", "auto"),
            parent_anchor_id=data.get("parent_anchor_id"),
            parent_history_version=data.get("parent_history_version"),
            parent_fingerprint=data.get("parent_fingerprint"),
            allow_fallback_to_full=data.get("allow_fallback_to_full", True),
            schema_version=data.get("schema_version", "relay-continuation-plan/1"),
            plan_hash=data.get("plan_hash", ""),
            topology=data.get("topology"),
        )
