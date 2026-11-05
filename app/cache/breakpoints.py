from __future__ import annotations

from typing import Any


def prepare_breakpoint_binding(*, plan: dict[str, Any], context_plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cfg = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
    max_breakpoints = int(cfg.get("max_breakpoints") or 1)
    if cfg.get("native_breakpoints") is True:
        # Only whole blocks whose positions the native adapter can prove.
        # In-message attachments stay in their original User content.
        composite = (plan.get("tracks") or {}).get("compatibility") == "composite_layered"
        limit = max(1, min(max_breakpoints, 3 if composite else 4))
        domains = [
            segment["domain"] for segment in context_plan.get("segments") or []
            if segment.get("domain") == "instructions"
            or (segment.get("domain") == "history" and context_plan.get("history_version", 0) > 0)
        ]
        return tuple({"placement": "after_segment", "domain": domain, "native": True}
                     for domain in domains[-limit:])
    anchors: list[dict[str, Any]] = []
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
