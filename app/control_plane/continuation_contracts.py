from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ContinuationAnchorClass = Literal[
    "none",
    "immutable_tree_anchor",       # 不可变树状锚点：Gemini Interactions / OpenAI & Grok Responses
    "linear_ephemeral_cursor",     # 受限线性游标：仅支持紧邻上一轮续接、不支持历史分叉的网关 context_id
    "mutable_session_container",   # 可变会话容器：远端有状态 Thread/Session，写入即原地变更远端容器状态
]

ContinuationMode = Literal[
    "off",                         # 禁用状态续接，强制 Stateless 全量投影
    "prefer_stateful_anchor",      # 优先选用最长合法父锚点续接；无合法锚点时在 not_sent 无损回退 Stateless
    "require_stateful_anchor",     # 要求供应具备已认证续接合同（首轮无历史允许创世锚点，合同缺失则 Fail-Closed）
]

VALID_ANCHOR_CLASSES = frozenset({
    "none",
    "immutable_tree_anchor",
    "linear_ephemeral_cursor",
    "mutable_session_container",
})

IMPLEMENTED_CONTINUATION_PROTOCOLS = frozenset({
    "gemini_interactions",
})


@dataclass(frozen=True)
class ContinuationCapabilityContract:
    """挂装于 CapabilityContract.continuation 的供应级续接能力合同"""
    version: str = "relay-continuation-contract/1"
    anchor_class: ContinuationAnchorClass = "none"
    verification_status: Literal["unverified", "verified", "disabled"] = "disabled"
    supports_branching: bool = False
    supports_multiturn_delta: bool = False
    requires_upstream_store: bool = True
    forbid_when_zero_retention: bool = True
    mutually_exclusive_cache_mechanisms: tuple[str, ...] = ()
    retention_ttl_seconds: int = 86400
    anchor_safety_margin_seconds: int = 30
    head_invariants: tuple[str, ...] = ("instructions", "tools", "account_scope_id", "model")
    re_specify_head_on_delta: bool = True
    requires_thought_signature_on_replay: bool = True
    max_delta_turns: int = 64

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


@dataclass(frozen=True)
class ContinuationPolicySpec:
    """与 CachePolicySpec 对称的续接治理策略，由 ModelOffering.continuation_policy_id 引用"""
    policy_id: str
    revision: str
    default_mode: ContinuationMode = "prefer_stateful_anchor"
    allow_request_override: bool = False
    allow_stateless_fallback_pre_dispatch: bool = True
    allow_head_mismatch_reopen: bool = True
    invalidate_on_affinity_rotation: bool = False
    max_active_anchors_per_session: int = 128
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


def validate_continuation_cross_contract(
    *, connection: Any, capability: Any, profile: Any, policy: Any | None,
) -> None:
    """编译期与准入期续接能力交叉核验门禁"""
    cont = capability.continuation or {}
    anchor_class = str(cont.get("anchor_class") or "none")
    if anchor_class not in VALID_ANCHOR_CLASSES:
        raise ValueError(f"Unknown continuation anchor_class: {anchor_class!r}")
    if anchor_class in {"linear_ephemeral_cursor", "mutable_session_container"} and cont.get("supports_branching") is True:
        raise ValueError(f"{anchor_class} cannot declare supports_branching=True")

    default_mode = getattr(policy, "default_mode", "off") if policy is not None else "off"
    if default_mode != "off" or anchor_class != "none":
        if connection.protocol not in IMPLEMENTED_CONTINUATION_PROTOCOLS:
            raise ValueError(f"Protocol {connection.protocol!r} has no certified continuation implementation")
        if profile is None or not (profile.continuation or {}).get("request_anchor_field"):
            raise ValueError(f"Protocol profile for {connection.protocol!r} lacks continuation wire mapping")
        profile_class = (profile.continuation or {}).get("anchor_class")
        if profile_class and profile_class != anchor_class:
            raise ValueError("Capability and protocol profile disagree on continuation anchor_class")

    # 显式缓存互斥门禁（例如 Gemini Interactions 严禁同时开启 stateful_resource）
    exclusive = set(cont.get("mutually_exclusive_cache_mechanisms") or ())
    if connection.protocol == "gemini_interactions":
        exclusive.add("stateful_resource")
    cap_cache = set((capability.cache or {}).get("supported_mechanisms") or ())
    prof_cache = set((profile.cache or {}).get("supported_mechanisms") or ()) if profile else set()
    conflicts = sorted(exclusive & (cap_cache | prof_cache))
    if conflicts:
        raise ValueError(f"Continuation mutually exclusive cache mechanisms enabled: {conflicts}")

    # ZDR 零数据保留静态门禁
    zdr = bool(
        (connection.metadata or {}).get("zero_data_retention")
        or (capability.metadata or {}).get("zero_data_retention")
    )
    if zdr and default_mode == "require_stateful_anchor" and cont.get("requires_upstream_store", True):
        raise ValueError("CONTINUATION_FORBIDDEN_BY_ZDR_POLICY: ZDR supply cannot require upstream stateful anchor")
