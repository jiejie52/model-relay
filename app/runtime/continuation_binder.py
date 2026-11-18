from __future__ import annotations

from datetime import datetime, timezone
import inspect
import logging
from typing import Any

from app.protocol_adapters.wire_projection.continuation_wire import slice_continuation_history
from app.runtime.continuation_contracts import ContinuationExecutionBinding
from app.runtime.contracts import RuntimeState

logger = logging.getLogger("model-relay-runtime")


class ContinuationBinder:
    """Binds continuation anchors and resolves incremental delta turns at physical runtime."""

    def __init__(self, repo, settings: Any = None):
        self.repo = repo
        self.settings = settings

    async def bind_for_context(self, state: RuntimeState) -> ContinuationExecutionBinding:
        """Resolve continuation intent, validate anchor validity and expiry, and bind delta history."""
        # 1. Read continuation intent plan
        intent_plan = getattr(state, "continuation_intent_plan", None)
        if intent_plan is None and hasattr(state, "plan"):
            intent_plan = getattr(state.plan, "continuation_intent_plan", None)
        if intent_plan is None and isinstance(getattr(state, "snapshot", None), dict):
            intent_plan = state.snapshot.get("continuation_intent_plan")
        if intent_plan is None and isinstance(getattr(state, "context_plan", None), dict):
            intent_plan = state.context_plan.get("continuation_intent_plan")

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

        return True
