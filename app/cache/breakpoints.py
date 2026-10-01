from __future__ import annotations

from typing import Any


def prepare_breakpoint_binding(*, plan: dict[str, Any], context_plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cfg = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
    max_breakpoints = int(cfg.get("max_breakpoints") or 1)
    anchors: list[dict[str, Any]] = []
    # One-phase implementation intentionally anchors only pre-existing semantic
    # boundaries. It does not reorder, split or rewrite content.
    for segment_id in context_plan.get("stable_prefix_segment_ids") or []:
        anchors.append(
            {
                "segment_id": str(segment_id),
                "placement": "after_segment",
                "ttl": cfg.get("ttl"),
            }
        )
    if max_breakpoints > 0:
        anchors = anchors[-max_breakpoints:]
    return tuple(anchors)
