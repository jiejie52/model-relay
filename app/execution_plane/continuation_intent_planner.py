"""Continuation intent planning and anchoring eligibility resolver."""
from __future__ import annotations

from typing import Any

from app.core.continuation_fingerprint import (
    CONTRACT_DRIVEN_HEAD,
    INTERACTIONS_HISTORY_HEAD,
    LEGACY_CONTINUATION_HEAD,
    continuation_head,
    native_prefix_chain,
)
from app.control_plane.continuation_contracts import IMPLEMENTED_CONTINUATION_PROTOCOLS
from app.execution_plane.continuation_contracts import ContinuationDecisionError, ContinuationIntentPlan


class ContinuationIntentPlanner:
    """Pure, admission-time planner for continuation intent.

    Assesses protocol support, cache mutual exclusivity, and candidate anchor fingerprint
    validity without invoking provider APIs or querying database state.
    """

    @classmethod
    def plan_frozen_context(
        cls, *, snapshot: dict[str, Any], session: dict[str, Any], request_policy: str,
        history: list[dict[str, Any]], model_history_visible: bool, body_mode: str | None,
        cache_mechanism: str | None,
    ) -> ContinuationIntentPlan:
        contract = (snapshot.get("capability_contract") or {}).get("continuation") or {}
        scope_is_bound = bool(snapshot.get("account_scope_id") and snapshot.get("connection_spec_hash"))

        route_meta = (session.get("metadata") or {}).get("_relay_route") or {}
        policy = snapshot.get("continuation_policy") or route_meta.get("continuation_policy") or {}
        default_mode = policy.get("default_mode", "prefer_stateful_anchor") if policy else "off"
        allow_override = policy.get("allow_request_override", False) if policy else False

        raw_req_mode = body_mode or session.get("continuation_mode")
        if raw_req_mode and not allow_override and raw_req_mode != default_mode:
            raise ContinuationDecisionError(
                "CONTINUATION_OVERRIDE_FORBIDDEN",
                "Request override of continuation_mode is not authorized by policy",
            )
        requested_mode = raw_req_mode if (allow_override and raw_req_mode) else default_mode

        mode_str = str(requested_mode or "off").strip().lower()
        if mode_str in {"off", "none", "disabled", "false"}:
            return ContinuationIntentPlan(
                continuation_mode="off",
                resolution_status="disabled",
                decision_reason="continuation_mode_off",
                requested_mode="off",
                schema_version="relay-continuation-plan/2",
            )

        # 1. ZDR (Zero Data Retention) check
        zdr = bool(
            (session.get("metadata") or {}).get("zero_data_retention")
            or (snapshot.get("capability_contract") or {}).get("zero_data_retention")
            or snapshot.get("zero_data_retention")
        )
        if zdr and contract.get("requires_upstream_store", True):
            if requested_mode == "require_stateful_anchor":
                raise ContinuationDecisionError(
                    "CONTINUATION_FORBIDDEN_BY_ZDR_POLICY",
                    "ZDR policy forbids server-side stateful continuation",
                )
            return ContinuationIntentPlan(
                continuation_mode="off",
                resolution_status="disabled",
                decision_reason="zdr_forbidden",
                requested_mode=requested_mode,
                schema_version="relay-continuation-plan/2",
            )

        # 2. Protocol and certified capability check
        protocol = snapshot.get("protocol")
        anchor_class = contract.get("anchor_class", "none")
        verified = contract.get("verification_status") == "verified"
        if not (
            protocol in IMPLEMENTED_CONTINUATION_PROTOCOLS
            and scope_is_bound
            and anchor_class in {"immutable_tree_anchor", "linear_ephemeral_cursor", "mutable_session_container"}
            and verified
            and contract.get("requires_upstream_store") is True
            and int(contract.get("retention_ttl_seconds") or 0) > 0
        ):
            if requested_mode == "require_stateful_anchor":
                raise ContinuationDecisionError(
                    "CONTINUATION_REQUIREMENT_UNSATISFIED",
                    f"Protocol {protocol!r} or offering lacks verified continuation capability",
                )
            return ContinuationIntentPlan(
                continuation_mode="off",
                resolution_status="disabled",
                decision_reason="not_supported_or_verified",
                requested_mode=requested_mode,
                schema_version="relay-continuation-plan/2",
            )

        # 3. Cache mutual exclusivity check
        exclusive_cache = set(contract.get("mutually_exclusive_cache_mechanisms") or [])
        if protocol == "gemini_interactions":
            exclusive_cache.add("stateful_resource")
        if cache_mechanism and cache_mechanism in exclusive_cache:
            if requested_mode == "require_stateful_anchor":
                raise ContinuationDecisionError(
                    "CONTINUATION_CACHE_MUTUAL_EXCLUSION",
                    f"Continuation conflicts with requested cache mechanism {cache_mechanism}",
                )
            return ContinuationIntentPlan(
                continuation_mode="off",
                resolution_status="disabled",
                decision_reason="mutually_exclusive_cache",
                requested_mode=requested_mode,
                schema_version="relay-continuation-plan/2",
            )

        # 4. Continuation is enabled - compute topology and reusability
        reusable = (
            model_history_visible
            and request_policy == "conversation"
            and session.get("context_policy") == "conversation"
            and session.get("history_mode", "cumulative") == "cumulative"
        )
        head_format = snapshot.get("head_format") or CONTRACT_DRIVEN_HEAD
        head = continuation_head(snapshot, head_format=head_format)
        topology = {
            "head_format": head_format,
            "context_policy": request_policy,
            "model_history_visible": model_history_visible,
            "visible_turn_count": len(history),
            "reusable_on_commit": reusable,
            "parent_head_fingerprint": head,
            "turn_prefix_chain": native_prefix_chain(head, history),
            "anchor_class": anchor_class,
            "supports_multiturn_delta": contract.get("supports_multiturn_delta") is True,
            "retention_ttl_seconds": int(contract.get("retention_ttl_seconds") or 86400),
            "anchor_safety_margin_seconds": int(contract.get("anchor_safety_margin_seconds") or 30),
            "max_delta_turns": int(contract.get("max_delta_turns") or 64),
            "invalidate_on_affinity_rotation": policy.get("invalidate_on_affinity_rotation") is True if policy else False,
        }
        return ContinuationIntentPlan(
            continuation_mode="auto" if reusable else "off",
            resolution_status="pending_runtime_bind" if reusable else "disabled",
            decision_reason="candidate_pending_runtime_bind" if reusable else "non_cumulative_or_invisible",
            planned_anchor_class=anchor_class,
            requested_mode=requested_mode,
            schema_version="relay-continuation-plan/2",
            topology=topology,
        )

    @classmethod
    def plan_continuation_intent(
        cls,
        session: dict[str, Any],
        body_mode: str | None,
        candidate_anchor: Any,
        history: list[dict[str, Any]],
        route_profile: Any,
    ) -> ContinuationIntentPlan:
        """Deprecated legacy compatibility facade redirecting to pure frozen context planning."""
        route_dict = route_profile if isinstance(route_profile, dict) else (session.get("metadata") or {}).get("_relay_route") or {}
        snapshot_mock = {
            "protocol": route_dict.get("protocol"),
            "capability_contract": route_dict.get("capability_contract"),
            "connection_spec_hash": route_dict.get("connection_spec_hash"),
            "account_scope_id": route_dict.get("account_scope_id"),
            "continuation_policy": route_dict.get("continuation_policy"),
        }
        return cls.plan_frozen_context(
            snapshot=snapshot_mock,
            session=session,
            request_policy=session.get("context_policy", "conversation"),
            history=history or [],
            model_history_visible=True,
            body_mode=body_mode,
            cache_mechanism=None,
        )
