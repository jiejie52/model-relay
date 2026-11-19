"""Continuation intent planning and anchoring eligibility resolver."""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

from app.core.continuation_fingerprint import (
    build_parent_head_fingerprint,
    verify_anchor_fingerprint,
)
from app.execution_plane.continuation_contracts import ContinuationIntentPlan


class ContinuationIntentPlanner:
    """Pure, admission-time planner for continuation intent.

    Assesses protocol support, cache mutual exclusivity, and candidate anchor fingerprint
    validity without invoking provider APIs or mutating database state.
    """

    @classmethod
    def plan_frozen_context(
        cls, *, snapshot: dict[str, Any], session: dict[str, Any], request_policy: str,
        history: list[dict[str, Any]], model_history_visible: bool, body_mode: str | None,
        cache_mechanism: str | None,
    ) -> ContinuationIntentPlan:
        from app.core.continuation_fingerprint import continuation_head, native_prefix_chain

        contract = (snapshot.get("capability_contract") or {}).get("continuation") or {}
        scope_is_bound = bool(snapshot.get("account_scope_id") and snapshot.get("connection_spec_hash"))
        mode_off = str(body_mode or session.get("continuation_mode") or "auto").strip().lower() in {
            "off", "none", "disabled", "false",
        }
        enabled = (
            snapshot.get("protocol") == "gemini_interactions" and scope_is_bound
            and contract.get("anchor_class") == "immutable_tree_anchor"
            and contract.get("verification_status") == "verified"
            and contract.get("requires_upstream_store") is True
            and int(contract.get("retention_ttl_seconds") or 0) > 0
            and not ((session.get("metadata") or {}).get("zero_data_retention")
                     or (snapshot.get("capability_contract") or {}).get("zero_data_retention"))
            and cache_mechanism not in (contract.get("mutually_exclusive_cache_mechanisms") or [])
            and not mode_off
        )
        reusable = (enabled and model_history_visible and request_policy == "conversation"
                    and session.get("context_policy") == "conversation"
                    and session.get("history_mode", "cumulative") == "cumulative")
        head = continuation_head(snapshot)
        topology = {
            "context_policy": request_policy,
            "model_history_visible": model_history_visible,
            "visible_turn_count": len(history),
            "reusable_on_commit": reusable,
            "parent_head_fingerprint": head,
            "turn_prefix_chain": native_prefix_chain(head, history),
            "anchor_class": contract.get("anchor_class") if enabled else None,
            "supports_multiturn_delta": contract.get("supports_multiturn_delta") is True,
            "retention_ttl_seconds": int(contract.get("retention_ttl_seconds") or 0),
            "anchor_safety_margin_seconds": int(contract.get("anchor_safety_margin_seconds") or 30),
            "max_delta_turns": int(contract.get("max_delta_turns") or 0),
        }
        return ContinuationIntentPlan(
            continuation_mode="auto" if reusable else "off",
            schema_version="relay-continuation-plan/2", topology=topology,
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
        """Plan continuation execution strategy for the incoming request."""
        # Normalize requested mode
        raw_mode = (body_mode or session.get("continuation_mode") or "auto")
        mode = str(raw_mode).strip().lower() if raw_mode else "auto"
        if mode in ("off", "none", "disabled", "false"):
            continuation_mode = "off"
        else:
            continuation_mode = "auto"

        contract, profile, protocol = cls._extract_route_info(route_profile, session)

        # 1. Capability & compatibility check
        if continuation_mode != "off":
            if cls._is_cache_mutually_exclusive(protocol, profile, contract, route_profile) and cls._is_cache_explicitly_requested(session):
                continuation_mode = "off"
            elif not cls._protocol_supports_continuation(protocol, profile, contract):
                continuation_mode = "off"

        # If ruled "off", return disabled plan
        if continuation_mode == "off":
            return ContinuationIntentPlan(
                continuation_mode="off",
                parent_anchor_id=None,
                parent_history_version=None,
                parent_fingerprint=None,
                allow_fallback_to_full=True,
            )

        # 2. Check candidate anchor if available
        matched_anchor_info = cls._verify_candidate_anchor(candidate_anchor, history, session)
        if matched_anchor_info is not None:
            anchor_id, anchor_version, anchor_fp = matched_anchor_info
            return ContinuationIntentPlan(
                continuation_mode="auto",
                parent_anchor_id=anchor_id,
                parent_history_version=anchor_version,
                parent_fingerprint=anchor_fp,
                allow_fallback_to_full=True,
            )

        # 3. No usable anchor -> Initial full plan
        instructions = session.get("instructions") or session.get("system_instructions")
        head_fp = build_parent_head_fingerprint(history, system_instructions=instructions) if history else None
        return ContinuationIntentPlan(
            continuation_mode="auto",
            parent_anchor_id=None,
            parent_history_version=None,
            parent_fingerprint=head_fp,
            allow_fallback_to_full=True,
        )

    @classmethod
    def _extract_route_info(
        cls, route_profile: Any, session: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        capability_contract = None
        protocol_profile = None
        protocol = None

        if isinstance(route_profile, dict):
            capability_contract = route_profile.get("capability_contract")
            protocol_profile = route_profile.get("protocol_profile")
            protocol = route_profile.get("protocol")
        elif route_profile is not None:
            capability_contract = getattr(route_profile, "capability_contract", None)
            protocol_profile = getattr(route_profile, "protocol_profile", None)
            protocol = getattr(route_profile, "protocol", None)

        if not capability_contract or not protocol_profile:
            session_route = (session.get("metadata") or {}).get("_relay_route") or session.get("route_profile") or {}
            if isinstance(session_route, dict):
                capability_contract = capability_contract or session_route.get("capability_contract")
                protocol_profile = protocol_profile or session_route.get("protocol_profile")
                protocol = protocol or session_route.get("protocol")

        if not protocol and isinstance(protocol_profile, dict):
            protocol = protocol_profile.get("protocol")
        if not protocol and isinstance(session, dict):
            protocol = session.get("protocol")

        return (
            capability_contract if isinstance(capability_contract, dict) else {},
            protocol_profile if isinstance(protocol_profile, dict) else {},
            str(protocol) if protocol else None,
        )

    @classmethod
    def _protocol_supports_continuation(
        cls, protocol: str | None, profile: dict[str, Any], contract: dict[str, Any]
    ) -> bool:
        if profile.get("supports_continuation") is False:
            return False
        if profile.get("continuation_supported") is False:
            return False
        if isinstance(profile.get("continuation"), dict) and profile["continuation"].get("enabled") is False:
            return False

        if contract.get("supports_continuation") is False:
            return False
        if contract.get("continuation_supported") is False:
            return False
        if isinstance(contract.get("continuation"), dict) and contract["continuation"].get("enabled") is False:
            return False

        unsupported_protocols = {"unsupported", "raw_completion", "text_completion"}
        if protocol and protocol.lower() in unsupported_protocols:
            return False

        return True

    @classmethod
    def _is_cache_mutually_exclusive(
        cls, protocol: str | None, profile: dict[str, Any], contract: dict[str, Any], route_profile: Any
    ) -> bool:
        if profile.get("continuation_cache_mutual_exclusive") is True:
            return True
        if contract.get("continuation_cache_mutual_exclusive") is True:
            return True
        if isinstance(profile.get("continuation"), dict) and profile["continuation"].get("mutually_exclusive_with_cache") is True:
            return True
        if isinstance(contract.get("continuation"), dict) and contract["continuation"].get("mutually_exclusive_with_cache") is True:
            return True
        if isinstance(profile.get("cache"), dict) and profile["cache"].get("mutually_exclusive_with_continuation") is True:
            return True
        if isinstance(contract.get("cache"), dict) and contract["cache"].get("mutually_exclusive_with_continuation") is True:
            return True

        if isinstance(route_profile, dict):
            if route_profile.get("cache_continuation_compatibility") == "mutually_exclusive":
                return True
            if route_profile.get("continuation_cache_mutually_exclusive") is True:
                return True

        if protocol == "gemini_native":
            return True

        return False

    @classmethod
    def _is_cache_explicitly_requested(cls, session: dict[str, Any]) -> bool:
        req_cache_mode = session.get("requested_cache_mode") or session.get("cache_mode")
        if req_cache_mode == "on":
            return True
        if session.get("requested_cache_mechanism_explicit"):
            if req_cache_mode != "off":
                return True
        return False

    @classmethod
    def _is_expired(cls, anchor: Any) -> bool:
        expires_at = anchor.get("expires_at") if isinstance(anchor, dict) else getattr(anchor, "expires_at", None)
        if expires_at is None:
            return False
        if isinstance(expires_at, (int, float)):
            return time.time() > expires_at
        if isinstance(expires_at, datetime):
            now = datetime.now(timezone.utc) if expires_at.tzinfo else datetime.utcnow()
            return now > expires_at
        if isinstance(expires_at, str):
            try:
                dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc) if dt.tzinfo else datetime.utcnow()
                return now > dt
            except Exception:
                pass
        return False

    @classmethod
    def _verify_candidate_anchor(
        cls, candidate_anchor: Any, history: list[dict[str, Any]], session: dict[str, Any]
    ) -> tuple[str, int, str] | None:
        if not candidate_anchor or not history:
            return None

        anchor_id = candidate_anchor.get("anchor_id") if isinstance(candidate_anchor, dict) else getattr(candidate_anchor, "anchor_id", None)
        if not anchor_id or not isinstance(anchor_id, str):
            return None

        raw_version = candidate_anchor.get("history_version") if isinstance(candidate_anchor, dict) else getattr(candidate_anchor, "history_version", None)
        try:
            anchor_version = int(raw_version) if raw_version is not None else len(history)
        except (ValueError, TypeError):
            return None

        if cls._is_expired(candidate_anchor):
            return None

        if not (0 < anchor_version <= len(history)):
            return None

        prefix_history = history[:anchor_version]
        instructions = session.get("instructions") or session.get("system_instructions")

        fp_with_inst = build_parent_head_fingerprint(prefix_history, system_instructions=instructions)
        if verify_anchor_fingerprint(candidate_anchor, fp_with_inst):
            return (anchor_id, anchor_version, fp_with_inst)

        if instructions:
            fp_without_inst = build_parent_head_fingerprint(prefix_history, system_instructions=None)
            if verify_anchor_fingerprint(candidate_anchor, fp_without_inst):
                return (anchor_id, anchor_version, fp_without_inst)

        return None
