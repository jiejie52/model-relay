from __future__ import annotations

from typing import Any


def prepare_breakpoint_binding(*, plan: dict[str, Any], context_plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cfg = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
    projection = plan.get("primary_projection") if isinstance(plan.get("primary_projection"), dict) else {}
    selected_ids = projection.get("segment_ids")
    allowed_ids = {
        str(value) for value in (context_plan.get("stable_prefix_segment_ids") or []) if str(value)
    }
    if isinstance(selected_ids, list):
        ordered_ids = [str(value) for value in selected_ids if str(value) in allowed_ids]
    else:
        ordered_ids = [str(value) for value in (context_plan.get("stable_prefix_segment_ids") or []) if str(value)]
    max_breakpoints = int(cfg.get("max_breakpoints") or 1)
    anchors: list[dict[str, Any]] = []
    # Only existing semantic boundaries may be selected. The planner does not
    # reorder, split or rewrite canonical content to manufacture breakpoints.
    for segment_id in ordered_ids:
        anchors.append(
            {
                "segment_id": segment_id,
                "placement": "after_segment",
                "ttl": cfg.get("ttl"),
                "binding_id": str((plan.get("binding_plan") or {}).get("binding_id") or "primary"),
            }
        )
    if max_breakpoints > 0:
        anchors = anchors[-max_breakpoints:]
    return tuple(anchors)
