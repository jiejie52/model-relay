"""Continuation intent planning and anchoring eligibility resolver."""
from __future__ import annotations

from typing import Any

from app.control_plane.continuation_contracts import IMPLEMENTED_CONTINUATION_PROTOCOLS
from app.core.continuation_fingerprint import (
    CONTRACT_DRIVEN_HEAD,
    compute_parent_head_fingerprint,
    native_prefix_chain,
)
from app.execution_plane.continuation_contracts import (
    ContinuationDecisionError,
    ContinuationIntentPlan,
)


class ContinuationIntentPlanner:
    """Pure, admission-time planner for continuation intent.

    Assesses protocol support, policy authorization, ZDR constraints, and cache
    mutual exclusivity using frozen control-plane contracts without invoking
    provider APIs or querying/mutating database state.
    """

    @classmethod
    def plan(
        cls,
        *,
        snapshot: dict[str, Any],
        session: dict[str, Any],
        request_policy: str,
        history: list[dict[str, Any]],
        model_history_visible: bool,
        body_mode: str | None = None,
        cache_mechanism: str | None = None,
        context_plan: dict[str, Any] | None = None,
    ) -> ContinuationIntentPlan:
        cap_contract = snapshot.get("capability_contract") if isinstance(snapshot.get("capability_contract"), dict) else {}
        contract = (
            snapshot.get("continuation_contract")
            or cap_contract.get("continuation")
            or {}
        )
        if not isinstance(contract, dict):
            contract = {}
        session_route = ((session.get("metadata") or {}).get("_relay_route") or {}) if isinstance(session, dict) else {}
        policy = (
            snapshot.get("continuation_policy")
            or session_route.get("continuation_policy")
            or {}
        )
        if not isinstance(policy, dict):
            policy = {}

        if policy:
            default_mode = str(policy.get("default_mode") or "off").strip().lower()
            allow_override = bool(policy.get("allow_request_override", False))
        else:
            default_mode = (
                "prefer_stateful_anchor"
                if (snapshot.get("protocol") == "gemini_interactions" and contract.get("verification_status") == "verified")
                else "off"
            )
            allow_override = False

        raw_req_mode = body_mode or session.get("continuation_mode")
        norm_req_mode = str(raw_req_mode).strip().lower() if raw_req_mode is not None and str(raw_req_mode).strip() else None
        if norm_req_mode is not None and not allow_override and norm_req_mode != default_mode:
            raise ContinuationDecisionError(
                "CONTINUATION_OVERRIDE_FORBIDDEN",
                "Request override of continuation_mode is not authorized by policy",
            )
        requested_mode = norm_req_mode if (allow_override and norm_req_mode) else default_mode

        if requested_mode in {"off", "none", "disabled", "false"}:
            return ContinuationIntentPlan(
                continuation_mode="off",
                resolution_status="disabled",
                decision_reason="continuation_mode_off",
                requested_mode="off",
                schema_version="relay-continuation-plan/2",
            )

        policy_meta = policy.get("metadata") if isinstance(policy.get("metadata"), dict) else {}
        cap_meta = cap_contract.get("metadata") if isinstance(cap_contract.get("metadata"), dict) else {}
        zdr = bool(
            (session.get("metadata") or {}).get("zero_data_retention")
            or cap_contract.get("zero_data_retention")
            or cap_meta.get("zero_data_retention")
            or policy_meta.get("zero_data_retention")
            or snapshot.get("zero_data_retention")
        )
        retention = snapshot.get("upstream_retention") or {}
        zdr = zdr or retention.get("reason") == "zdr_forbidden"
        if (zdr or retention and retention.get("allow_store") is not True) and contract.get("requires_upstream_store", True):
            if requested_mode == "require_stateful_anchor":
                raise ContinuationDecisionError(
                    "CONTINUATION_FORBIDDEN_BY_ZDR_POLICY" if zdr else "CONTINUATION_STORAGE_POLICY_UNVERIFIED",
                    "Continuation storage is not authorized for this scope",
                )
            return ContinuationIntentPlan(
                continuation_mode="off",
                resolution_status="disabled",
                decision_reason="zdr_forbidden" if zdr else "storage_not_authorized",
                requested_mode=requested_mode,
                schema_version="relay-continuation-plan/2",
            )

        protocol = snapshot.get("protocol")
        scope_is_bound = bool(snapshot.get("account_scope_id") and snapshot.get("connection_spec_hash"))
        anchor_class = str(contract.get("anchor_class") or "none")
        capability_ok = (
            protocol in IMPLEMENTED_CONTINUATION_PROTOCOLS
            and scope_is_bound
            and anchor_class in {"immutable_tree_anchor", "linear_ephemeral_cursor", "mutable_session_container"}
            and contract.get("verification_status") == "verified"
            and contract.get("requires_upstream_store") is True
            and int(contract.get("retention_ttl_seconds") or 0) > 0
        )
        if not capability_ok:
            if requested_mode == "require_stateful_anchor":
                raise ContinuationDecisionError(
                    "CONTINUATION_REQUIREMENT_UNSATISFIED",
                    f"Protocol {protocol!r} or offering lacks verified continuation capability",
                )
            return ContinuationIntentPlan(
                continuation_mode="off",
                resolution_status="disabled",
                decision_reason="continuation_not_enabled",
                requested_mode=requested_mode,
                schema_version="relay-continuation-plan/2",
            )

        exclusive = set(contract.get("mutually_exclusive_cache_mechanisms") or ())
        if protocol == "gemini_interactions":
            exclusive.add("stateful_resource")
        if cache_mechanism and cache_mechanism in exclusive:
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

        reusable = bool(
            model_history_visible
            and request_policy == "conversation"
            and session.get("context_policy") == "conversation"
            and session.get("history_mode", "cumulative") == "cumulative"
        )
        ctx_topology = (context_plan or {}).get("continuation_topology") if isinstance(context_plan, dict) else None
        if isinstance(ctx_topology, dict) and ctx_topology.get("parent_head_fingerprint") is not None:
            head_format = ctx_topology.get("head_format") or snapshot.get("head_format") or CONTRACT_DRIVEN_HEAD
            head = ctx_topology["parent_head_fingerprint"]
            visible_turn_count = int(ctx_topology.get("visible_turn_count", 0))
            chain = list(ctx_topology.get("turn_prefix_chain") or [])
        else:
            head_format = snapshot.get("head_format") or CONTRACT_DRIVEN_HEAD
            head = compute_parent_head_fingerprint(snapshot, head_format=head_format)
            visible_hist = history if model_history_visible else []
            visible_turn_count = len(visible_hist)
            chain = native_prefix_chain(head, visible_hist)

        topology = {
            "head_format": head_format,
            "context_policy": request_policy,
            "history_mode": str(session.get("history_mode") or "cumulative"),
            "model_history_visible": model_history_visible,
            "visible_turn_count": visible_turn_count,
            "reusable_on_commit": reusable,
            "parent_head_fingerprint": head,
            "turn_prefix_chain": chain,
            "anchor_class": anchor_class,
            "supports_branching": contract.get("supports_branching") is True,
            "supports_multiturn_delta": contract.get("supports_multiturn_delta") is True,
            "retention_ttl_seconds": int(contract.get("retention_ttl_seconds") or 86400),
            "anchor_safety_margin_seconds": int(contract.get("anchor_safety_margin_seconds") or 30),
            "max_delta_turns": int(contract.get("max_delta_turns", 64)),
            "invalidate_on_affinity_rotation": bool(policy.get("invalidate_on_affinity_rotation", False)),
            "allow_head_mismatch_reopen": bool(policy.get("allow_head_mismatch_reopen", True)),
            "max_active_anchors_per_session": max(1, int(policy.get("max_active_anchors_per_session") or 128)),
        }
        return ContinuationIntentPlan(
            continuation_mode="auto" if reusable else "off",
            resolution_status="pending_runtime_bind" if reusable else "disabled",
            decision_reason="candidate_pending_runtime_bind" if reusable else "non_cumulative_or_invisible",
            planned_anchor_class=anchor_class,
            requested_mode=requested_mode,
            allow_fallback_to_full=bool(policy.get("allow_stateless_fallback_pre_dispatch", True)),
            schema_version="relay-continuation-plan/2",
            topology=topology,
        )

    @classmethod
    def plan_frozen_context(
        cls, *, snapshot: dict[str, Any], session: dict[str, Any], request_policy: str,
        history: list[dict[str, Any]], model_history_visible: bool, body_mode: str | None = None,
        cache_mechanism: str | None = None, context_plan: dict[str, Any] | None = None,
    ) -> ContinuationIntentPlan:
        return cls.plan(
            snapshot=snapshot,
            session=session,
            request_policy=request_policy,
            history=history,
            model_history_visible=model_history_visible,
            body_mode=body_mode,
            cache_mechanism=cache_mechanism,
            context_plan=context_plan,
        )

    @classmethod
    def plan_continuation_intent(
        cls,
        session: dict[str, Any],
        body_mode: str | None,
        candidate_anchor: dict[str, Any] | Any | None,
        history: list[dict[str, Any]],
        route_profile: dict[str, Any] | Any,
    ) -> ContinuationIntentPlan:
        """Pure facade delegating to plan(...) with zero DB/anchor lookup."""
        del candidate_anchor
        session_route = (
            ((session.get("metadata") or {}).get("_relay_route") or session.get("route_profile") or {})
            if isinstance(session, dict)
            else {}
        )
        if not isinstance(session_route, dict):
            session_route = {}

        def _pick(key: str, default: Any = None) -> Any:
            if isinstance(route_profile, dict) and key in route_profile and route_profile[key] is not None:
                return route_profile[key]
            if route_profile is not None and not isinstance(route_profile, dict):
                val = getattr(route_profile, key, None)
                if val is not None:
                    return val
            if key in session_route and session_route[key] is not None:
                return session_route[key]
            if isinstance(session, dict) and key in session and session[key] is not None:
                return session[key]
            return default

        capability_contract = _pick("capability_contract")
        protocol_profile = _pick("protocol_profile")
        protocol = _pick("protocol")
        if not protocol and isinstance(protocol_profile, dict):
            protocol = protocol_profile.get("protocol")
        request_policy = str((session or {}).get("context_policy") or "conversation")
        model_history_visible = bool(
            request_policy == "conversation"
            and (session or {}).get("history_mode", "cumulative") != "ephemeral_turn"
        )
        snapshot: dict[str, Any] = {
            "session_id": (session or {}).get("id") or (session or {}).get("session_id"),
            "tenant_id": (session or {}).get("tenant_id"),
            "conversation_hash": (session or {}).get("conversation_hash"),
            "connection_id": _pick("connection_id"),
            "account_scope_id": _pick("account_scope_id"),
            "connection_spec_hash": _pick("connection_spec_hash"),
            "protocol_profile_hash": _pick("protocol_profile_hash"),
            "model": _pick("model"),
            "upstream_model": _pick("upstream_model"),
            "protocol": str(protocol) if protocol else None,
            "capability_contract": capability_contract if isinstance(capability_contract, dict) else {},
            "continuation_contract": _pick("continuation_contract"),
            "continuation_policy": _pick("continuation_policy"),
            "instructions": (session or {}).get("instructions") or (session or {}).get("system_instructions"),
            "tools": (session or {}).get("tools"),
            "zero_data_retention": _pick("zero_data_retention"),
        }
        cache_mechanism = None
        if isinstance(session, dict):
            req_cache_mode = session.get("requested_cache_mode") or session.get("cache_mode")
            if req_cache_mode == "on" or (session.get("requested_cache_mechanism_explicit") and req_cache_mode != "off"):
                cache_mechanism = session.get("requested_cache_mechanism")
        return cls.plan(
            snapshot=snapshot,
            session=session if isinstance(session, dict) else {},
            request_policy=request_policy,
            history=history or [],
            model_history_visible=model_history_visible,
            body_mode=body_mode,
            cache_mechanism=cache_mechanism,
            context_plan=None,
        )
