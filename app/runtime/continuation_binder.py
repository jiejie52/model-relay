from __future__ import annotations

from datetime import datetime, timezone
import inspect
import logging
from typing import Any

from app.core.continuation_fingerprint import (
    LEGACY_CONTINUATION_HEAD, continuation_head, continuation_material_references, native_prefix_chain,
)
from app.core.material_placement import verify_parent_chain_material_coverage
from app.protocol_adapters.base import ProviderRequestError
from app.protocol_adapters.wire_projection.continuation_wire import slice_continuation_history
from app.runtime.continuation_contracts import ContinuationExecutionBinding
from app.runtime.contracts import RuntimeState

logger = logging.getLogger("model-relay-runtime")


class ContinuationBinder:
    """Binds continuation anchors and resolves incremental delta turns at physical runtime."""

    def __init__(self, repo, settings: Any = None):
        self.repo = repo
        self.settings = settings

    async def bind_for_context(self, state: RuntimeState, *, reopen=False) -> ContinuationExecutionBinding:
        """Resolve continuation intent, validate anchor validity and expiry, and bind delta history."""
        # 1. Read continuation intent plan
        intent_plan = getattr(state, "continuation_intent_plan", None)
        if intent_plan is None and hasattr(state, "plan"):
            intent_plan = getattr(state.plan, "continuation_intent_plan", None)
        if intent_plan is None and isinstance(getattr(state, "snapshot", None), dict):
            intent_plan = state.snapshot.get("continuation_intent_plan")
        if intent_plan is None and isinstance(getattr(state, "context_plan", None), dict):
            intent_plan = state.context_plan.get("continuation_intent_plan")

        policy = state.snapshot.get("context_policy") or state.session.get("context_policy")
        non_cumulative = (policy in ("explicit", "session_materials_only")
                          or state.snapshot.get("model_history_visible") is False
                          or state.session.get("history_mode") == "ephemeral_turn")
        if isinstance(intent_plan, dict) and intent_plan.get("schema_version") == "relay-continuation-plan/2":
            return await self._bind_frozen(state, intent_plan, reopen=reopen)
        if non_cumulative or state.snapshot.get("protocol") == "gemini_interactions":
            state.continuation_binding = ContinuationExecutionBinding(
                schema_version="relay-continuation-binding/2", effective_mode="stateless_full_replay",
            )
            state.continuation_anchor = None
            return state.continuation_binding

        continuation_mode = "auto"
        parent_anchor_id = None
        parent_history_version = None

        if intent_plan is not None:
            if isinstance(intent_plan, dict):
                continuation_mode = intent_plan.get("continuation_mode", "auto")
                parent_anchor_id = intent_plan.get("parent_anchor_id")
                parent_history_version = intent_plan.get("parent_history_version")
            else:
                continuation_mode = getattr(intent_plan, "continuation_mode", "auto")
                parent_anchor_id = getattr(intent_plan, "parent_anchor_id", None)
                parent_history_version = getattr(intent_plan, "parent_history_version", None)

        if not parent_anchor_id and isinstance(getattr(state, "snapshot", None), dict):
            parent_anchor_id = state.snapshot.get("parent_anchor_id")

        mode_str = str(continuation_mode or "").strip().lower()
        is_off = mode_str in ("off", "none", "disabled", "false")

        # 2. Disabled or empty parent_anchor_id -> mechanism="none", active_anchor_id=None
        if is_off or not parent_anchor_id:
            binding = ContinuationExecutionBinding(
                mechanism="none",
                active_anchor_id=None,
                upstream_handle=None,
                bound_delta_turns=[],
                fallback_occurred=False,
                fallback_reason=None,
            )
            state.continuation_binding = binding
            state.continuation_anchor = None
            if isinstance(getattr(state, "snapshot", None), dict):
                state.snapshot["continuation_binding"] = binding.to_dict()
            return binding

        # 3. parent_anchor_id exists -> query repo and validate
        anchor = None
        if self.repo is not None and hasattr(self.repo, "get_continuation_anchor"):
            try:
                res = self.repo.get_continuation_anchor(parent_anchor_id)
                anchor = await res if inspect.isawaitable(res) else res
            except Exception as exc:
                logger.warning(
                    "failed_to_fetch_continuation_anchor: %s",
                    exc,
                    extra={"anchor_id": str(parent_anchor_id)},
                )
                anchor = None

        # Resolve safety_seconds
        safety_seconds = 30
        if self.settings is not None:
            safety_seconds = getattr(
                self.settings,
                "continuation_safety_seconds",
                getattr(self.settings, "cache_expiry_safety_seconds", 30),
            )
        if isinstance(getattr(state, "snapshot", None), dict):
            if "continuation_safety_seconds" in state.snapshot:
                safety_seconds = int(state.snapshot["continuation_safety_seconds"])
            elif "historical_material_safety_seconds" in state.snapshot:
                safety_seconds = int(state.snapshot["historical_material_safety_seconds"])

        # Validate anchor
        if self._is_anchor_valid(anchor, state, safety_seconds):
            anchor_id = str(
                anchor.get("id") or anchor.get("anchor_id")
                if isinstance(anchor, dict)
                else getattr(anchor, "anchor_id", parent_anchor_id)
            )
            upstream_handle = (
                anchor.get("upstream_handle")
                if isinstance(anchor, dict)
                else getattr(anchor, "upstream_handle", None)
            )
            mechanism = (
                (anchor.get("mechanism") if isinstance(anchor, dict) else getattr(anchor, "mechanism", None))
                or "upstream_handle"
            )

            cov_ver = (
                anchor.get("history_version")
                if isinstance(anchor, dict)
                else getattr(anchor, "history_version", None)
            )
            if cov_ver is None:
                cov_ver = parent_history_version
            try:
                cov_ver_int = int(cov_ver) if cov_ver is not None else 0
            except (ValueError, TypeError):
                cov_ver_int = 0

            history = state.history if hasattr(state, "history") and state.history else []
            bound_delta_turns = slice_continuation_history(history, cov_ver_int, allow_fallback=True)

            binding = ContinuationExecutionBinding(
                mechanism=mechanism,
                active_anchor_id=anchor_id,
                upstream_handle=upstream_handle,
                bound_delta_turns=bound_delta_turns,
                fallback_occurred=False,
                fallback_reason=None,
            )
            state.continuation_binding = binding
            state.continuation_anchor = anchor
            if isinstance(getattr(state, "snapshot", None), dict):
                state.snapshot["continuation_binding"] = binding.to_dict()
                state.snapshot["continuation_anchor"] = (
                    anchor.to_dict() if hasattr(anchor, "to_dict") else anchor
                )
            return binding

        # 4. Fallback to full history
        binding = ContinuationExecutionBinding(
            mechanism="none",
            active_anchor_id=None,
            upstream_handle=None,
            bound_delta_turns=[],
            fallback_occurred=True,
            fallback_reason="ANCHOR_EXPIRED_OR_INVALID",
        )
        state.continuation_binding = binding
        state.continuation_anchor = None
        if isinstance(getattr(state, "snapshot", None), dict):
            state.snapshot["continuation_binding"] = binding.to_dict()
            state.snapshot.pop("continuation_anchor", None)
        return binding

    async def _bind_frozen(self, state, intent, *, reopen):
        topology = intent.get("topology") or {}
        history = state.history
        try:
            head = continuation_head(state.snapshot, head_format=topology.get("head_format", LEGACY_CONTINUATION_HEAD))
        except ValueError as exc:
            raise ProviderRequestError("CONTINUATION_CONTEXT_MISMATCH", str(exc)) from exc
        chain = native_prefix_chain(head, history)
        if (topology.get("parent_head_fingerprint") != head
                or topology.get("visible_turn_count") != len(history)
                or topology.get("turn_prefix_chain") != chain):
            raise ProviderRequestError("CONTINUATION_CONTEXT_MISMATCH", "Frozen continuation topology differs from visible context")

        placement = state.snapshot.get("material_placement_plan")
        if state.snapshot.get("protocol") == "gemini_interactions" and not placement and any(
            "fileData" in part
            for turn in history
            for part in (turn.get("transport_history", {}).get("user_content") or {}).get("parts", [])
        ):
            raise ProviderRequestError("HISTORICAL_MATERIAL_UNPINNED", "Interactions replay requires a verified ledger for historical file references")

        can_store = (topology.get("reusable_on_commit") is True and
                     (placement is not None or not state.material_bindings))
        rejections: list[dict[str, Any]] = []
        selected_candidate = None
        anchor_class = topology.get("anchor_class") or "immutable_tree_anchor"
        invalidate_on_affinity = bool(topology.get("invalidate_on_affinity_rotation"))
        current_affinity = (state.wire or {}).get("affinity_epoch")

        if can_store and history and not reopen:
            refs = continuation_material_references((placement or {}).get("verified_history_occurrences") or [])
            live = {(b["material_id"], b["binding_generation"], b.get("provider_binding_id"), b.get("external_uri"))
                    for b in state.historical_pinned_bindings}
            candidates = await self.repo.list_continuation_prefixes(
                session_id=state.session_id, tenant_id=state.request_row["tenant_id"],
                conversation_hash=state.request_row["conversation_hash"], head=head, max_turns=len(history),
            )
            for candidate in candidates:
                aid = candidate.get("id") or candidate.get("anchor_id")
                covered = candidate.get("covered_turn_count")
                handle = candidate.get("upstream_handle")
                if not self._is_anchor_valid(candidate, state, topology.get("anchor_safety_margin_seconds", 30)):
                    rejections.append({"anchor_id": str(aid), "reason": "scope_state_or_ttl_invalid"})
                    continue
                if invalidate_on_affinity and candidate.get("affinity_epoch") != current_affinity:
                    rejections.append({"anchor_id": str(aid), "reason": "affinity_epoch_rotated"})
                    continue
                if type(covered) is not int or not 1 <= covered <= len(history):
                    rejections.append({"anchor_id": str(aid), "reason": "turn_count_out_of_bounds"})
                    continue
                if not isinstance(handle, str) or not handle.strip():
                    rejections.append({"anchor_id": str(aid), "reason": "upstream_handle_empty"})
                    continue
                if anchor_class in {"linear_ephemeral_cursor", "mutable_session_container"} and (
                    covered != len(history) or int(candidate.get("history_version") or 0) != int(state.session.get("history_version") or 0)
                ):
                    rejections.append({"anchor_id": str(aid), "reason": "linear_continuity_broken"})
                    continue
                if anchor_class == "mutable_session_container":
                    if hasattr(self.repo, "has_unreconciled_indeterminate_request") and await self.repo.has_unreconciled_indeterminate_request(state.session_id):
                        rejections.append({"anchor_id": str(aid), "reason": "mutable_container_tainted"})
                        continue
                if candidate.get("turn_chain_digest") != chain[covered - 1]:
                    rejections.append({"anchor_id": str(aid), "reason": "not_true_ordered_prefix"})
                    continue
                if len(history) - covered > topology.get("max_delta_turns", 64):
                    rejections.append({"anchor_id": str(aid), "reason": "exceeds_max_delta_turns"})
                    continue
                if covered < len(history) and not topology.get("supports_multiturn_delta", False):
                    rejections.append({"anchor_id": str(aid), "reason": "multiturn_delta_not_supported"})
                    continue
                covered_refs = [ref for ref in refs if ref["history_entry_index"] < covered]
                if candidate.get("covered_material_bindings") != covered_refs or any(
                    (ref["material_id"], ref["binding_generation"], ref["provider_binding_id"], ref["external_uri"]) not in live
                    for ref in covered_refs
                ):
                    rejections.append({"anchor_id": str(aid), "reason": "ANCHOR_MATERIAL_GENERATION_MISMATCH"})
                    continue
                if placement is not None:
                    cov_ok, cov_reason = verify_parent_chain_material_coverage(
                        placement_plan=placement,
                        anchor_material_bindings=covered_refs,
                        delta_turns=history[covered:],
                    )
                    if not cov_ok:
                        rejections.append({"anchor_id": str(aid), "reason": cov_reason or "material_coverage_gap"})
                        continue
                selected_candidate = candidate
                break

        if selected_candidate is not None:
            covered = selected_candidate.get("covered_turn_count")
            handle = selected_candidate.get("upstream_handle")
            binding = ContinuationExecutionBinding(
                schema_version="relay-continuation-binding/2",
                effective_mode="stateful_anchor_delta",
                mechanism=anchor_class,
                anchor_class=anchor_class,
                active_anchor_id=selected_candidate["id"],
                upstream_handle=handle,
                covered_turn_count=covered,
                delta_start_turn_index=covered,
                delta_end_turn_index=len(history),
                store_upstream_state=True,
                parent_head_fingerprint=head,
                covered_turn_chain_hash=chain[covered - 1],
                decision_reason="matched_longest_true_prefix",
                rejected_candidates_audit=rejections,
            )
        else:
            binding = ContinuationExecutionBinding(
                schema_version="relay-continuation-binding/2",
                effective_mode="stateful_root_init" if can_store else "stateless_full_replay",
                anchor_class=anchor_class if can_store else "none",
                store_upstream_state=can_store,
                parent_head_fingerprint=head,
                delta_start_turn_index=0,
                delta_end_turn_index=len(history),
                fallback_occurred=bool(can_store and history),
                fallback_reason="NO_VALID_PARENT_ANCHOR" if can_store and history else None,
                decision_reason="reopen_stateless" if reopen else ("genesis_turn" if not history else "no_valid_parent_fallback"),
                rejected_candidates_audit=rejections,
            )

        state.continuation_binding = binding
        state.continuation_anchor = None
        return binding

    def _is_anchor_valid(self, anchor: Any, state: RuntimeState, safety_seconds: int) -> bool:
        """Confirm anchor state is active, not expired within safety margin, and scope matches."""
        if not anchor:
            return False

        # Status check
        anchor_state = anchor.get("state") if isinstance(anchor, dict) else getattr(anchor, "state", None)
        if anchor_state is not None and anchor_state != "active":
            return False

        # Expiry check
        expires_at = anchor.get("expires_at") if isinstance(anchor, dict) else getattr(anchor, "expires_at", None)
        if expires_at is not None:
            now_ts = datetime.now(timezone.utc).timestamp()
            exp_ts = None
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
                    return False

            if exp_ts is not None and exp_ts <= (now_ts + max(0, safety_seconds)):
                return False

        # Session scope check
        anchor_session = (
            anchor.get("session_id")
            if isinstance(anchor, dict)
            else getattr(anchor, "session_id", None)
        )
        if anchor_session and str(anchor_session) != str(state.session_id):
            return False

        # Tenant scope check
        anchor_tenant = (
            anchor.get("tenant_id")
            if isinstance(anchor, dict)
            else getattr(anchor, "tenant_id", None)
        )
        req_tenant = (
            (state.request_row or {}).get("tenant_id")
            if isinstance(state.request_row, dict)
            else None
        )
        if anchor_tenant and req_tenant and str(anchor_tenant) != str(req_tenant):
            return False

        # Conversation scope check
        anchor_conv = (
            anchor.get("conversation_hash")
            if isinstance(anchor, dict)
            else getattr(anchor, "conversation_hash", None)
        )
        req_conv = (
            (state.request_row or {}).get("conversation_hash")
            if isinstance(state.request_row, dict)
            else None
        )
        if anchor_conv and req_conv and str(anchor_conv) != str(req_conv):
            return False

        # Connection ID check
        anchor_conn = (
            anchor.get("connection_id")
            if isinstance(anchor, dict)
            else getattr(anchor, "connection_id", None)
        )
        req_conn = state.snapshot.get("connection_id")
        if anchor_conn and req_conn and str(anchor_conn) != str(req_conn):
            return False

        # Connection spec hash check
        anchor_spec = (
            anchor.get("connection_spec_hash")
            if isinstance(anchor, dict)
            else getattr(anchor, "connection_spec_hash", None)
        )
        req_spec = state.snapshot.get("connection_spec_hash")
        if anchor_spec and req_spec and str(anchor_spec) != str(req_spec):
            return False

        # Account scope check
        anchor_scope = (
            anchor.get("account_scope_hash")
            if isinstance(anchor, dict)
            else getattr(anchor, "account_scope_hash", None)
        )
        req_scope = state.snapshot.get("account_scope_id")
        if anchor_scope and req_scope and str(anchor_scope) != str(req_scope):
            return False

        return True
