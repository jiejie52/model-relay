from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from app.core.continuation_fingerprint import (
    CONTRACT_DRIVEN_HEAD, continuation_head, continuation_material_references, native_prefix_chain,
)
from app.core.material_placement import verify_parent_chain_material_coverage
from app.protocol_adapters.base import ProviderRequestError
from app.runtime.continuation_contracts import ContinuationExecutionBinding
from app.runtime.contracts import RuntimeState

logger = logging.getLogger("model-relay-runtime")


def _anchor_field(anchor: Any, key: str, default: Any = None) -> Any:
    if isinstance(anchor, dict):
        return anchor.get(key, default)
    return getattr(anchor, key, default)


class ContinuationBinder:
    """Binds continuation anchors and resolves incremental delta turns at physical runtime."""

    def __init__(self, repo, settings: Any = None):
        self.repo = repo
        self.settings = settings

    async def bind_for_context(self, state: RuntimeState, *, reopen: bool = False) -> ContinuationExecutionBinding:
        """Resolve continuation intent, validate anchor validity and expiry, and bind delta history."""
        intent_plan = getattr(state, "continuation_intent_plan", None)
        if intent_plan is None and hasattr(state, "plan"):
            intent_plan = getattr(state.plan, "continuation_intent_plan", None)
        if intent_plan is None and isinstance(getattr(state, "snapshot", None), dict):
            intent_plan = state.snapshot.get("continuation_intent_plan")
        if intent_plan is None and isinstance(getattr(state, "context_plan", None), dict):
            intent_plan = state.context_plan.get("continuation_intent_plan")
        if hasattr(intent_plan, "to_dict"):
            intent_plan = intent_plan.to_dict()

        if isinstance(intent_plan, dict) and intent_plan.get("schema_version") == "relay-continuation-plan/2":
            if (
                intent_plan.get("resolution_status") == "disabled"
                and intent_plan.get("continuation_mode") == "off"
                and not ((intent_plan.get("topology") or {}) or state.snapshot.get("protocol") == "gemini_interactions")
            ):
                state.continuation_binding = ContinuationExecutionBinding(
                    schema_version="relay-continuation-binding/2",
                    effective_mode="disabled",
                    mechanism="none",
                    decision_reason=str(intent_plan.get("decision_reason") or "disabled"),
                )
                state.continuation_anchor = None
                return state.continuation_binding
            return await self._bind_frozen(state, intent_plan, reopen=reopen)

        if state.snapshot.get("protocol") == "gemini_interactions":
            state.continuation_binding = ContinuationExecutionBinding(
                schema_version="relay-continuation-binding/2",
                effective_mode="stateless_full_replay",
                mechanism="none",
                decision_reason="stateless_full_replay",
            )
        else:
            state.continuation_binding = ContinuationExecutionBinding(
                schema_version="relay-continuation-binding/2",
                effective_mode="disabled",
                mechanism="none",
                decision_reason="disabled",
            )
        state.continuation_anchor = None
        return state.continuation_binding

    async def _bind_frozen(self, state: RuntimeState, intent: dict[str, Any], *, reopen: bool) -> ContinuationExecutionBinding:
        topology = intent.get("topology") or {}
        if intent.get("resolution_status") == "disabled" and not topology:
            state.continuation_binding = ContinuationExecutionBinding(
                schema_version="relay-continuation-binding/2",
                effective_mode="disabled",
                mechanism="none",
                decision_reason=str(intent.get("decision_reason") or "disabled"),
            )
            state.continuation_anchor = None
            return state.continuation_binding

        history = state.history
        anchor_class = str(
            topology.get("anchor_class")
            or ((state.snapshot.get("capability_contract") or {}).get("continuation") or {}).get("anchor_class")
            or "none"
        )
        try:
            head = continuation_head(state.snapshot, head_format=topology.get("head_format", CONTRACT_DRIVEN_HEAD))
        except ValueError as exc:
            if intent.get("allow_fallback_to_full", True):
                state.continuation_binding = ContinuationExecutionBinding(
                    schema_version="relay-continuation-binding/2",
                    effective_mode="stateless_full_replay",
                    store_upstream_state=False,
                    fallback_occurred=True,
                    fallback_reason="HEAD_FORMAT_UNSUPPORTED",
                    anchor_class=anchor_class,
                    delta_start_turn_index=0,
                    delta_end_turn_index=len(history),
                    decision_reason="HEAD_FORMAT_UNSUPPORTED",
                )
                state.continuation_anchor = None
                return state.continuation_binding
            raise ProviderRequestError("CONTINUATION_CONTEXT_MISMATCH", str(exc)) from exc
        chain = native_prefix_chain(head, history)
        if (topology.get("parent_head_fingerprint") != head
                or topology.get("visible_turn_count") != len(history)
                or topology.get("turn_prefix_chain") != chain):
            if intent.get("allow_fallback_to_full", True):
                logger.warning(
                    "continuation_topology_mismatch_fallback: expected_head=%s, actual_head=%s, expected_turns=%s, actual_turns=%s",
                    topology.get("parent_head_fingerprint"),
                    head,
                    topology.get("visible_turn_count"),
                    len(history),
                    extra={"request_id": state.request_id},
                )
                state.continuation_binding = ContinuationExecutionBinding(
                    schema_version="relay-continuation-binding/2",
                    effective_mode="stateless_full_replay",
                    store_upstream_state=False,
                    parent_head_fingerprint=head,
                    fallback_occurred=True,
                    fallback_reason="CONTINUATION_CONTEXT_MISMATCH",
                    anchor_class=anchor_class,
                    delta_start_turn_index=0,
                    delta_end_turn_index=len(history),
                    decision_reason="CONTINUATION_CONTEXT_MISMATCH",
                )
                state.continuation_anchor = None
                return state.continuation_binding
            raise ProviderRequestError("CONTINUATION_CONTEXT_MISMATCH", "Frozen continuation topology differs from visible context")

        placement = state.snapshot.get("material_placement_plan")
        if state.snapshot.get("protocol") == "gemini_interactions" and not placement and any(
            "fileData" in part
            for turn in history
            for part in (turn.get("transport_history", {}).get("user_content") or {}).get("parts", [])
        ):
            raise ProviderRequestError("HISTORICAL_MATERIAL_UNPINNED", "Interactions replay requires a verified ledger for historical file references")

        active_retention = (state.wire or {}).get("upstream_retention")
        frozen_retention = state.snapshot.get("upstream_retention")
        storage_denied = any(record is not None and record.get("allow_store") is not True
                             for record in (active_retention, frozen_retention))
        if storage_denied and topology.get("reusable_on_commit"):
            if (intent.get("requested_mode") == "require_stateful_anchor"
                    or not intent.get("allow_fallback_to_full", True)):
                raise ProviderRequestError("CONTINUATION_STORAGE_NOT_AUTHORIZED", "Current retention policy forbids a stored response")
        can_store = (
            not storage_denied
            and topology.get("reusable_on_commit") is True
            and (placement is not None or not state.material_bindings)
        )
        rejections: list[dict[str, Any]] = []
        matched_binding: ContinuationExecutionBinding | None = None
        head_diverged = False
        is_mutable_container = anchor_class == "mutable_session_container"
        mutable_container_tainted = False
        if (
            is_mutable_container
            and can_store
            and state.session_id
            and hasattr(self.repo, "has_tainted_mutable_container")
        ):
            req_row = state.request_row if isinstance(state.request_row, dict) else {}
            mutable_container_tainted = bool(
                await self.repo.has_tainted_mutable_container(
                    session_id=state.session_id,
                    tenant_id=req_row.get("tenant_id"),
                    conversation_hash=req_row.get("conversation_hash"),
                    exclude_request_id=state.request_id,
                )
            )
            if mutable_container_tainted:
                rejections.append({"reason": "mutable_container_tainted"})

        if can_store and history and not reopen and not mutable_container_tainted:
            refs = continuation_material_references((placement or {}).get("verified_history_occurrences") or [])
            live = {
                (b["material_id"], b["binding_generation"], b.get("provider_binding_id"), b.get("external_uri"))
                for b in state.historical_pinned_bindings
            }
            candidates = await self.repo.list_continuation_prefixes(
                session_id=state.session_id,
                tenant_id=state.request_row["tenant_id"],
                conversation_hash=state.request_row["conversation_hash"],
                head=head,
                max_turns=len(history),
            )
            safety_seconds = int(topology.get("anchor_safety_margin_seconds") or 0)
            max_delta_turns = int(topology.get("max_delta_turns") or 0)
            supports_multiturn = bool(topology.get("supports_multiturn_delta"))
            current_epoch = (state.wire or {}).get("affinity_epoch")

            head_diverged = False
            valid_matches: list[tuple[int, int, dict[str, Any]]] = []
            for candidate in candidates:
                aid = _anchor_field(candidate, "id") or _anchor_field(candidate, "anchor_id") or ""

                # Gate 1: State, root prefix, scope, anchor class, and TTL safety margin
                if _anchor_field(candidate, "is_root_prefix") is False:
                    rejections.append({"anchor_id": str(aid), "reason": "not_active_root_prefix"})
                    continue
                cand_class = str(_anchor_field(candidate, "anchor_class") or "")
                if anchor_class != "none" and cand_class and cand_class != anchor_class:
                    rejections.append({"anchor_id": str(aid), "reason": "anchor_class_mismatch"})
                    continue
                validity_reason = self._anchor_validity_reason(candidate, state, safety_seconds)
                if validity_reason is not None:
                    rejections.append({"anchor_id": str(aid), "reason": validity_reason})
                    continue

                # Gate 1b: Parent head fingerprint match (instructions / tools / scope / model)
                cand_head = _anchor_field(candidate, "head_fingerprint") or _anchor_field(candidate, "parent_head_fingerprint")
                if cand_head != head:
                    head_diverged = True
                    rejections.append({"anchor_id": str(aid), "reason": "head_fingerprint_mismatch"})
                    continue

                # Gate 2: Affinity epoch check
                cand_epoch = candidate.get("affinity_epoch")
                if cand_epoch is None and isinstance(candidate.get("metadata"), dict):
                    cand_epoch = candidate["metadata"].get("affinity_epoch")
                if topology.get("invalidate_on_affinity_rotation") and cand_epoch != current_epoch:
                    rejections.append({"anchor_id": str(aid), "reason": "affinity_epoch_rotated"})
                    continue

                # Gate 3: Turn count bounds & linear/mutable cursor continuity
                covered = candidate.get("covered_turn_count")
                if type(covered) is not int or not 1 <= covered <= len(history):
                    rejections.append({"anchor_id": str(aid), "reason": "turn_count_out_of_bounds"})
                    continue

                handle = candidate.get("upstream_handle")
                if not isinstance(handle, str) or not handle.strip():
                    rejections.append({"anchor_id": str(aid), "reason": "upstream_handle_empty"})
                    continue

                if anchor_class in {"linear_ephemeral_cursor", "mutable_session_container"} and (
                    covered != len(history)
                    or int(candidate.get("history_version") or 0) != int(state.session.get("history_version") or 0)
                ):
                    rejections.append({"anchor_id": str(aid), "reason": "linear_continuity_broken"})
                    continue

                if len(history) - covered > max_delta_turns:
                    rejections.append({"anchor_id": str(aid), "reason": "exceeds_max_delta_turns"})
                    continue

                if covered < len(history) and not supports_multiturn:
                    rejections.append({"anchor_id": str(aid), "reason": "multiturn_delta_not_supported"})
                    continue

                # Gate 4: True ordered prefix chain digest
                if candidate.get("turn_chain_digest") != chain[covered - 1]:
                    rejections.append({"anchor_id": str(aid), "reason": "not_true_ordered_prefix"})
                    continue

                # Gate 5: Material binding_generation match against live pinned bindings
                covered_refs = [ref for ref in refs if ref["history_entry_index"] < covered]
                if candidate.get("covered_material_bindings") != covered_refs or any(
                    (ref["material_id"], ref["binding_generation"], ref["provider_binding_id"], ref["external_uri"]) not in live
                    for ref in covered_refs
                ):
                    rejections.append({"anchor_id": str(aid), "reason": "ANCHOR_MATERIAL_GENERATION_MISMATCH"})
                    continue

                # Gate 6: Parent chain material coverage
                if placement is not None:
                    cov_ok, cov_reason = verify_parent_chain_material_coverage(
                        placement_plan=placement,
                        anchor_material_bindings=covered_refs,
                        delta_turns=history[covered:],
                    )
                    if not cov_ok:
                        rejections.append({"anchor_id": str(aid), "reason": cov_reason or "material_coverage_gap"})
                        continue

                valid_matches.append((
                    covered,
                    int(candidate.get("history_version") or 0),
                    {"aid": str(aid), "handle": handle, "covered": covered},
                ))

            if valid_matches:
                _, _, best = max(valid_matches, key=lambda item: (item[0], item[1]))
                mechanism = anchor_class if anchor_class != "none" else "immutable_tree_anchor"
                matched_binding = ContinuationExecutionBinding(
                    schema_version="relay-continuation-binding/2",
                    effective_mode="stateful_anchor_delta",
                    mechanism=mechanism,
                    active_anchor_id=best["aid"],
                    upstream_handle=best["handle"],
                    covered_turn_count=best["covered"],
                    store_upstream_state=True,
                    parent_head_fingerprint=head,
                    covered_turn_chain_hash=chain[best["covered"] - 1],
                    anchor_class=anchor_class,
                    delta_start_turn_index=best["covered"],
                    delta_end_turn_index=len(history),
                    decision_reason="matched_longest_true_prefix",
                    rejected_candidates_audit=tuple(rejections[:20]),
                )

        if rejections or (can_store and history and not reopen and not mutable_container_tainted):
            if not isinstance(state.wire, dict):
                state.wire = {}
            state.wire["continuation_rejected_candidates"] = rejections[:20]

        if matched_binding is not None:
            binding = matched_binding
        else:
            if can_store and not topology.get("allow_head_mismatch_reopen", True):
                last_head = next((turn["continuation_head_fingerprint"] for turn in reversed(history)
                                  if turn.get("continuation_head_fingerprint")), None)
                if head_diverged or (last_head is not None and last_head != head):
                    raise ProviderRequestError(
                        "CONTINUATION_HEAD_MISMATCH_REOPEN_FORBIDDEN",
                        "Committed continuation head fingerprint diverged and reopen is forbidden by policy",
                    )
            allow_stateless_fallback = intent.get("allow_fallback_to_full", True)
            can_init_stateful_root = can_store
            if is_mutable_container:
                # mutable_session_container mutates server state in place and forbids
                # multi-turn root seeding or branching; when history exists without a
                # valid linear head anchor, or on taint/pre-dispatch reopen, fall back
                # to stateless_full_replay instead of opening a fresh stateful container.
                can_init_stateful_root = bool(
                    can_store
                    and not mutable_container_tainted
                    and not history
                    and not reopen
                )
                if can_store and not can_init_stateful_root and not allow_stateless_fallback:
                    if mutable_container_tainted:
                        raise ProviderRequestError(
                            "MUTABLE_CONTAINER_TAINTED",
                            "Mutable session container is tainted and stateless fallback is disallowed",
                        )
                    raise ProviderRequestError(
                        "CONTINUATION_PARENT_NO_LONGER_USABLE",
                        "No valid mutable session container parent anchor and stateless fallback is disallowed",
                    )

            effective_mode = "stateful_root_init" if can_init_stateful_root else "stateless_full_replay"
            if storage_denied:
                decision_reason = fallback_reason = "CONTINUATION_STORAGE_NOT_AUTHORIZED"
                fallback_occurred = bool(topology.get("reusable_on_commit"))
            elif mutable_container_tainted and can_store:
                decision_reason = "mutable_container_tainted"
                fallback_occurred = True
                fallback_reason = "MUTABLE_CONTAINER_TAINTED"
            elif reopen:
                decision_reason = "reopen_stateless"
                fallback_occurred = bool(can_store and (history or is_mutable_container))
                fallback_reason = "NO_VALID_PARENT_ANCHOR" if fallback_occurred else None
            elif not history:
                decision_reason = "genesis_turn"
                fallback_occurred = False
                fallback_reason = None
            elif head_diverged:
                decision_reason = "head_fingerprint_diverged_reopen"
                fallback_occurred = True
                fallback_reason = "HEAD_FINGERPRINT_MISMATCH"
            elif can_store:
                decision_reason = "no_valid_parent_fallback"
                fallback_occurred = True
                fallback_reason = "NO_VALID_PARENT_ANCHOR"
            else:
                decision_reason = str(intent.get("decision_reason") or "no_valid_parent_fallback")
                fallback_occurred = False
                fallback_reason = None
            binding = ContinuationExecutionBinding(
                schema_version="relay-continuation-binding/2",
                effective_mode=effective_mode,
                store_upstream_state=can_init_stateful_root,
                parent_head_fingerprint=head,
                fallback_occurred=fallback_occurred,
                fallback_reason=fallback_reason,
                anchor_class=anchor_class,
                delta_start_turn_index=0,
                delta_end_turn_index=len(history),
                decision_reason=decision_reason,
                rejected_candidates_audit=tuple(rejections[:20]),
            )

        state.continuation_binding = binding
        state.continuation_anchor = None
        return binding

    def _anchor_validity_reason(self, anchor: Any, state: RuntimeState, safety_seconds: int) -> str | None:
        """Return None when anchor state, scope, and TTL margin are valid, else a rejection reason."""
        if not anchor:
            return "scope_or_state_invalid"

        if _anchor_field(anchor, "state") != "active":
            return "scope_or_state_invalid"

        req_session = str(state.session_id or "")
        if req_session and str(_anchor_field(anchor, "session_id") or "") != req_session:
            return "scope_or_state_invalid"

        req_row = state.request_row if isinstance(state.request_row, dict) else {}
        snapshot = state.snapshot if isinstance(state.snapshot, dict) else {}
        session = state.session if isinstance(state.session, dict) else {}
        route = (session.get("metadata") or {}).get("_relay_route") or {}

        req_tenant = str(req_row.get("tenant_id") or snapshot.get("tenant_id") or "")
        if req_tenant and str(_anchor_field(anchor, "tenant_id") or "") != req_tenant:
            return "scope_or_state_invalid"

        req_conv = str(req_row.get("conversation_hash") or snapshot.get("conversation_hash") or "")
        if req_conv and str(_anchor_field(anchor, "conversation_hash") or "") != req_conv:
            return "scope_or_state_invalid"

        req_conn = str(snapshot.get("connection_id") or session.get("connection_id") or "")
        if req_conn and str(_anchor_field(anchor, "connection_id") or "") != req_conn:
            return "scope_or_state_invalid"

        req_spec = str(
            snapshot.get("connection_spec_hash")
            or getattr(state.plan, "connection_spec_hash", None)
            or route.get("connection_spec_hash")
            or ""
        )
        if req_spec and str(_anchor_field(anchor, "connection_spec_hash") or "") != req_spec:
            return "scope_or_state_invalid"

        req_scope = str(
            snapshot.get("account_scope_id")
            or snapshot.get("account_scope_hash")
            or getattr(state.plan, "account_scope_id", None)
            or route.get("account_scope_id")
            or route.get("account_scope_hash")
            or ""
        )
        anc_scope = _anchor_field(anchor, "account_scope_hash") or _anchor_field(anchor, "account_scope_id")
        if req_scope and str(anc_scope or "") != req_scope:
            return "scope_or_state_invalid"

        expires_at = _anchor_field(anchor, "expires_at")
        if expires_at is None:
            return "anchor_ttl_insufficient"

        now_ts = datetime.now(timezone.utc).timestamp()
        exp_ts: float | None = None
        if isinstance(expires_at, (int, float)):
            exp_ts = float(expires_at)
        elif isinstance(expires_at, datetime):
            exp_ts = (
                expires_at.timestamp()
                if expires_at.tzinfo
                else expires_at.replace(tzinfo=timezone.utc).timestamp()
            )
        elif isinstance(expires_at, str):
            try:
                dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                exp_ts = dt.timestamp()
            except Exception:
                return "anchor_ttl_insufficient"
        else:
            return "anchor_ttl_insufficient"

        if exp_ts <= (now_ts + max(0, int(safety_seconds or 0))):
            return "anchor_ttl_insufficient"

        return None

    def _is_anchor_valid(self, anchor: Any, state: RuntimeState, safety_seconds: int) -> bool:
        """Confirm anchor state is active, not expired within safety margin, and scope matches."""
        return self._anchor_validity_reason(anchor, state, safety_seconds) is None
