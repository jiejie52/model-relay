from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

IMPLEMENTED_CONTINUATION_PROTOCOLS = frozenset({"gemini_interactions", "responses"})
VALID_ANCHOR_CLASSES = frozenset({
    "none",
    "immutable_tree_anchor",
    "linear_ephemeral_cursor",
    "mutable_session_container",
})
DEFAULT_HEAD_INVARIANTS = ("instructions", "tools", "account_scope_id", "model")


@dataclass(frozen=True)
class ContinuationCapabilityContract:
    version: str = "relay-continuation-contract/1"
    anchor_class: str = "none"
    verification_status: str = "unspecified"
    supports_branching: bool = False
    supports_multiturn_delta: bool = False
    requires_upstream_store: bool = False
    forbid_when_zero_retention: bool = True
    mutually_exclusive_cache_mechanisms: tuple[str, ...] = ()
    retention_ttl_seconds: int = 0
    anchor_safety_margin_seconds: int = 30
    head_invariants: tuple[str, ...] = DEFAULT_HEAD_INVARIANTS
    re_specify_head_on_delta: bool = False
    requires_thought_signature_on_replay: bool = False
    max_delta_turns: int = 0

    def canonical(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "anchor_class": self.anchor_class,
            "verification_status": self.verification_status,
            "supports_branching": self.supports_branching,
            "supports_multiturn_delta": self.supports_multiturn_delta,
            "requires_upstream_store": self.requires_upstream_store,
            "forbid_when_zero_retention": self.forbid_when_zero_retention,
            "mutually_exclusive_cache_mechanisms": list(self.mutually_exclusive_cache_mechanisms),
            "retention_ttl_seconds": self.retention_ttl_seconds,
            "anchor_safety_margin_seconds": self.anchor_safety_margin_seconds,
            "head_invariants": list(self.head_invariants),
            "re_specify_head_on_delta": self.re_specify_head_on_delta,
            "requires_thought_signature_on_replay": self.requires_thought_signature_on_replay,
            "max_delta_turns": self.max_delta_turns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ContinuationCapabilityContract:
        if not data:
            return cls()
        return cls(
            version=str(data.get("version") or "relay-continuation-contract/1"),
            anchor_class=str(data.get("anchor_class") or "none"),
            verification_status=str(data.get("verification_status") or "unspecified"),
            supports_branching=bool(data.get("supports_branching", False)),
            supports_multiturn_delta=bool(data.get("supports_multiturn_delta", False)),
            requires_upstream_store=bool(data.get("requires_upstream_store", False)),
            forbid_when_zero_retention=bool(data.get("forbid_when_zero_retention", True)),
            mutually_exclusive_cache_mechanisms=tuple(data.get("mutually_exclusive_cache_mechanisms") or ()),
            retention_ttl_seconds=int(data.get("retention_ttl_seconds") or 0),
            anchor_safety_margin_seconds=int(data.get("anchor_safety_margin_seconds") or 30),
            head_invariants=tuple(data.get("head_invariants") or DEFAULT_HEAD_INVARIANTS),
            re_specify_head_on_delta=bool(data.get("re_specify_head_on_delta", False)),
            requires_thought_signature_on_replay=bool(data.get("requires_thought_signature_on_replay", False)),
            max_delta_turns=int(data.get("max_delta_turns") or 0),
        )


@dataclass(frozen=True)
class ContinuationPolicySpec:
    policy_id: str
    revision: str = "1"
    default_mode: str = "off"
    allow_request_override: bool = True
    allow_stateless_fallback_pre_dispatch: bool = True
    allow_head_mismatch_reopen: bool = True
    invalidate_on_affinity_rotation: bool = False
    max_active_anchors_per_session: int = 10
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "revision": self.revision,
            "default_mode": self.default_mode,
            "allow_request_override": self.allow_request_override,
            "allow_stateless_fallback_pre_dispatch": self.allow_stateless_fallback_pre_dispatch,
            "allow_head_mismatch_reopen": self.allow_head_mismatch_reopen,
            "invalidate_on_affinity_rotation": self.invalidate_on_affinity_rotation,
            "max_active_anchors_per_session": self.max_active_anchors_per_session,
            "metadata": self.metadata,
        }

    @property
    def policy_hash(self) -> str:
        from app.core.idempotency import stable_hash
        return stable_hash(self.canonical())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContinuationPolicySpec:
        return cls(
            policy_id=str(data["policy_id"]),
            revision=str(data.get("revision") or "1"),
            default_mode=str(data.get("default_mode") or "off"),
            allow_request_override=bool(data.get("allow_request_override", True)),
            allow_stateless_fallback_pre_dispatch=bool(data.get("allow_stateless_fallback_pre_dispatch", True)),
            allow_head_mismatch_reopen=bool(data.get("allow_head_mismatch_reopen", True)),
            invalidate_on_affinity_rotation=bool(data.get("invalidate_on_affinity_rotation", False)),
            max_active_anchors_per_session=int(data.get("max_active_anchors_per_session") or 10),
            metadata=dict(data.get("metadata") or {}),
        )


def validate_continuation_cross_contract(
    *, connection: Any, capability: Any, profile: Any, policy: Any | None = None,
) -> None:
    cont = getattr(capability, "continuation", {}) or {}
    if isinstance(cont, ContinuationCapabilityContract):
        cont = cont.canonical()
    anchor_class = str(cont.get("anchor_class") or "none")
    if anchor_class not in VALID_ANCHOR_CLASSES:
        raise ValueError(f"Unknown continuation anchor_class: {anchor_class!r}")
    if anchor_class in {"linear_ephemeral_cursor", "mutable_session_container"}:
        if cont.get("supports_branching") is True:
            raise ValueError(f"{anchor_class} cannot declare supports_branching=True")
        if cont.get("supports_multiturn_delta") is True:
            raise ValueError(f"{anchor_class} cannot declare supports_multiturn_delta=True")

    if anchor_class != "none" and cont.get("requires_upstream_store", True):
        ttl = int(cont.get("retention_ttl_seconds") or 0)
        margin = int(cont.get("anchor_safety_margin_seconds") or 0)
        if not (ttl > margin > 0):
            raise ValueError("Stateful continuation requires retention_ttl_seconds > anchor_safety_margin_seconds > 0")

    if isinstance(policy, dict):
        default_mode = str(policy.get("default_mode") or "off")
        policy_meta = policy.get("metadata") or {}
    elif policy is not None:
        default_mode = str(getattr(policy, "default_mode", "off") or "off")
        policy_meta = getattr(policy, "metadata", {}) or {}
    else:
        default_mode = "off"
        policy_meta = {}

    if default_mode not in {"off", "prefer_stateful_anchor", "require_stateful_anchor"}:
        raise ValueError(f"Unknown continuation policy default_mode: {default_mode!r}")

    if default_mode != "off" or anchor_class != "none":
        proto = getattr(connection, "protocol", None)
        if proto not in IMPLEMENTED_CONTINUATION_PROTOCOLS:
            raise ValueError(f"Protocol {proto!r} has no certified continuation implementation")
        prof_cont = (getattr(profile, "continuation", None) or {}) if profile else {}
        if not prof_cont.get("request_anchor_field") or not prof_cont.get("response_anchor_field"):
            raise ValueError(f"Protocol profile for {proto!r} lacks continuation wire mapping")
        profile_class = prof_cont.get("anchor_class")
        if profile_class and profile_class != anchor_class:
            raise ValueError("Capability and protocol profile disagree on continuation anchor_class")

    # Mutually exclusive cache mechanisms
    exclusive = set(cont.get("mutually_exclusive_cache_mechanisms") or ())
    if getattr(connection, "protocol", None) == "gemini_interactions":
        exclusive.add("stateful_resource")
    cap_cache = set((getattr(capability, "cache", {}) or {}).get("supported_mechanisms") or ())
    prof_cache = set((getattr(profile, "cache", {}) or {}).get("supported_mechanisms") or ()) if profile else set()
    conflicts = exclusive & (cap_cache | prof_cache)
    if conflicts:
        raise ValueError(f"Continuation mutually exclusive cache mechanisms enabled: {sorted(conflicts)}")

    # Zero Data Retention static gate
    conn_meta = getattr(connection, "metadata", {}) or {}
    cap_meta = getattr(capability, "metadata", {}) or {}
    zdr = bool(
        cont.get("zero_data_retention")
        or conn_meta.get("zero_data_retention")
        or cap_meta.get("zero_data_retention")
        or policy_meta.get("zero_data_retention")
    )
    if zdr and default_mode == "require_stateful_anchor" and cont.get("requires_upstream_store", True):
        raise ValueError("CONTINUATION_FORBIDDEN_BY_ZDR_POLICY: ZDR supply cannot require upstream stateful anchor")

