from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from app.core.idempotency import stable_hash
from app.immutable import freeze_json, thaw_json


@dataclass(frozen=True)
class ContinuationExecutionBinding:
    mechanism: str = "none"
    active_anchor_id: str | None = None
    upstream_handle: Any = None
    bound_delta_turns: list[dict[str, Any]] = field(default_factory=list)
    fallback_occurred: bool = False
    fallback_reason: str | None = None
    schema_version: str = "relay-continuation-binding/1"
    binding_hash: str = ""

    effective_mode: str | None = None
    store_upstream_state: bool = False
    covered_turn_count: int = 0
    parent_head_fingerprint: str | None = None
    covered_turn_chain_hash: str | None = None
    anchor_class: str = "none"
    delta_start_turn_index: int = 0
    delta_end_turn_index: int = 0
    decision_reason: str = "none"
    rejected_candidates_audit: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        for name in ("upstream_handle", "bound_delta_turns", "rejected_candidates_audit"):
            val = getattr(self, name)
            if val is not None:
                object.__setattr__(self, name, freeze_json(val))
        if not self.binding_hash:
            object.__setattr__(self, "binding_hash", stable_hash(self.canonical(include_hash=False)))

    def canonical(self, *, include_hash: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "mechanism": self.mechanism,
            "active_anchor_id": self.active_anchor_id,
            "upstream_handle": thaw_json(self.upstream_handle) if self.upstream_handle is not None else None,
            "bound_delta_turns": thaw_json(self.bound_delta_turns),
            "fallback_occurred": self.fallback_occurred,
            "fallback_reason": self.fallback_reason,
        }
        if self.effective_mode is not None:
            data.update(
                effective_mode=self.effective_mode,
                store_upstream_state=self.store_upstream_state,
                covered_turn_count=self.covered_turn_count,
                parent_head_fingerprint=self.parent_head_fingerprint,
                covered_turn_chain_hash=self.covered_turn_chain_hash,
                anchor_class=self.anchor_class,
                delta_start_turn_index=self.delta_start_turn_index,
                delta_end_turn_index=self.delta_end_turn_index,
                decision_reason=self.decision_reason,
                rejected_candidates_audit=thaw_json(self.rejected_candidates_audit),
            )
        if include_hash:
            data["binding_hash"] = self.binding_hash or stable_hash(data)
        return data

    def to_dict(self) -> dict[str, Any]:
        return self.canonical(include_hash=True)

    def public_summary(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "active_anchor_id": self.active_anchor_id,
            "fallback_occurred": self.fallback_occurred,
            "fallback_reason": self.fallback_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContinuationExecutionBinding:
        return cls(
            mechanism=data.get("mechanism", "none"),
            active_anchor_id=data.get("active_anchor_id"),
            upstream_handle=data.get("upstream_handle"),
            bound_delta_turns=list(data.get("bound_delta_turns") or []),
            fallback_occurred=data.get("fallback_occurred", False),
            fallback_reason=data.get("fallback_reason"),
            schema_version=data.get("schema_version", "relay-continuation-binding/1"),
            binding_hash=data.get("binding_hash", ""),
            effective_mode=data.get("effective_mode"),
            store_upstream_state=data.get("store_upstream_state") is True,
            covered_turn_count=data.get("covered_turn_count", 0),
            parent_head_fingerprint=data.get("parent_head_fingerprint"),
            covered_turn_chain_hash=data.get("covered_turn_chain_hash"),
            anchor_class=data.get("anchor_class", "none"),
            delta_start_turn_index=data.get("delta_start_turn_index", 0),
            delta_end_turn_index=data.get("delta_end_turn_index", 0),
            decision_reason=data.get("decision_reason", "none"),
            rejected_candidates_audit=list(data.get("rejected_candidates_audit") or []),
        )


@dataclass
class ContinuationAnchorRecord:
    anchor_id: str = ""
    session_id: str = ""
    history_version: int = 0
    fingerprint: str | None = None
    mechanism: str = ""
    upstream_handle: Any = None
    tenant_id: str = ""
    conversation_hash: str = ""
    turn_index: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "relay-continuation-anchor/1"
    anchor_hash: str = ""
    created_at: Any = None
    expires_at: Any = None

    def __post_init__(self):
        if not self.anchor_hash and self.anchor_id:
            raw = {
                "schema_version": self.schema_version,
                "anchor_id": self.anchor_id,
                "session_id": self.session_id,
                "history_version": self.history_version,
                "fingerprint": self.fingerprint,
                "mechanism": self.mechanism,
                "tenant_id": self.tenant_id,
                "conversation_hash": self.conversation_hash,
                "turn_index": self.turn_index,
            }
            self.anchor_hash = stable_hash(raw)

    def canonical(self, *, include_hash: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "anchor_id": self.anchor_id,
            "session_id": self.session_id,
            "history_version": self.history_version,
            "fingerprint": self.fingerprint,
            "mechanism": self.mechanism,
            "upstream_handle": thaw_json(self.upstream_handle) if self.upstream_handle is not None else None,
            "tenant_id": self.tenant_id,
            "conversation_hash": self.conversation_hash,
            "turn_index": self.turn_index,
            "metadata": thaw_json(self.metadata),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        if include_hash:
            data["anchor_hash"] = self.anchor_hash or stable_hash(data)
        return data

    def to_dict(self) -> dict[str, Any]:
        return self.canonical(include_hash=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContinuationAnchorRecord:
        return cls(
            anchor_id=data.get("anchor_id", ""),
            session_id=data.get("session_id", ""),
            history_version=data.get("history_version", 0),
            fingerprint=data.get("fingerprint"),
            mechanism=data.get("mechanism", ""),
            upstream_handle=data.get("upstream_handle"),
            tenant_id=data.get("tenant_id", ""),
            conversation_hash=data.get("conversation_hash", ""),
            turn_index=data.get("turn_index"),
            metadata=dict(data.get("metadata") or {}),
            schema_version=data.get("schema_version", "relay-continuation-anchor/1"),
            anchor_hash=data.get("anchor_hash", ""),
            created_at=data.get("created_at"),
            expires_at=data.get("expires_at"),
        )
