from __future__ import annotations

from typing import Any

from ..core.idempotency import stable_hash

_SUPPORTED_PROJECTION_KINDS = {
    "full_stable_prefix",
    "stable_segments",
    "material_set",
}


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    return {str(item).strip().lower() for item in values if str(item).strip()}


def freeze_cache_projection(
    *,
    spec: dict[str, Any] | None,
    context_plan: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a policy projection rule to ordered, immutable ContextPlan refs.

    This module never rewrites prompt content. It selects only existing stable
    ContextPlan segments and freezes their identities for execution adapters.
    """
    raw = dict(spec or {})
    kind = str(raw.get("kind") or "full_stable_prefix").strip().lower()
    if kind not in _SUPPORTED_PROJECTION_KINDS:
        raise ValueError(f"unsupported_cache_projection_kind:{kind}")

    segments = [item for item in context_plan.get("segments", []) if isinstance(item, dict)]
    stable_ids = [
        str(value) for value in (context_plan.get("stable_prefix_segment_ids") or []) if str(value)
    ]
    stable_set = set(stable_ids)
    by_id = {str(item.get("segment_id") or ""): item for item in segments if item.get("segment_id")}
    stable_segments = [by_id[value] for value in stable_ids if value in by_id]
    # Older frozen ContextPlan fixtures may carry stable IDs but omit the
    # segment catalog. Preserve the historical full-prefix projection for
    # those plans; richer material/domain selectors require the v2 catalog.
    if not stable_segments and stable_ids and kind == "full_stable_prefix":
        stable_segments = [
            {"segment_id": value, "domain": "legacy_stable_segment", "ordinal": index}
            for index, value in enumerate(stable_ids)
        ]

    selected: list[dict[str, Any]]
    if kind == "full_stable_prefix":
        selected = list(stable_segments)
    elif kind == "stable_segments":
        requested_ids = raw.get("segment_ids")
        domains = _string_set(raw.get("domains"))
        stability = _string_set(raw.get("stability"))
        exclude_domains = _string_set(raw.get("exclude_domains"))
        if requested_ids is not None:
            requested = {str(value) for value in requested_ids if str(value)}
            unknown = requested - stable_set
            if unknown:
                raise ValueError("cache_projection_contains_nonstable_or_unknown_segments")
            if not stable_segments:
                selected = [{"segment_id": value, "domain": "legacy_stable_segment", "ordinal": index} for index, value in enumerate(stable_ids) if value in requested]
            else:
                selected = [item for item in stable_segments if str(item.get("segment_id")) in requested]
        else:
            selected = [
                item for item in stable_segments
                if (not domains or str(item.get("domain") or "").lower() in domains)
                and (not stability or str(item.get("stability") or "").lower() in stability)
                and str(item.get("domain") or "").lower() not in exclude_domains
            ]
    else:  # material_set
        material_segments = [item for item in stable_segments if str(item.get("domain") or "").lower() == "material"]
        requested_ids = raw.get("material_ids")
        wanted_ids = {str(value) for value in (requested_ids if isinstance(requested_ids, (list, tuple, set)) else [requested_ids] if isinstance(requested_ids, str) else []) if str(value)}
        selectors = raw.get("selectors") if isinstance(raw.get("selectors"), dict) else {}
        content_types = _string_set(selectors.get("content_types"))
        extensions = {value.lstrip(".") for value in _string_set(selectors.get("extensions"))}
        stability = _string_set(selectors.get("stability"))
        selected = []
        for item in material_segments:
            material_id = str(item.get("source_ref") or "")
            filename = str(item.get("filename") or "")
            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            content_type = str(item.get("content_type") or "").lower()
            if wanted_ids and material_id not in wanted_ids:
                continue
            if content_types and content_type not in content_types:
                continue
            if extensions and extension not in extensions:
                continue
            if stability and str(item.get("stability") or "").lower() not in stability:
                continue
            if not wanted_ids and not content_types and not extensions and not stability:
                continue
            selected.append(item)

    selected_ids = [str(item.get("segment_id")) for item in selected]
    selected_material_ids = [
        str(item.get("source_ref"))
        for item in selected
        if str(item.get("domain") or "").lower() == "material" and item.get("source_ref")
    ]
    if not selected_ids:
        raise ValueError("cache_projection_empty")

    projection = {
        "schema_version": "relay-cache-projection-plan/1",
        "kind": kind,
        "segment_ids": selected_ids,
        "material_ids": selected_material_ids,
        "domains": list(dict.fromkeys(str(item.get("domain") or "") for item in selected)),
        "semantic_order_preserved": True,
        "context_plan_hash": context_plan.get("context_plan_hash"),
        "selected_segment_fingerprints": [
            {
                "segment_id": item.get("segment_id"),
                "domain": item.get("domain"),
                "role": item.get("role"),
                "canonical_content_hash": item.get("canonical_content_hash"),
                "material_representation_digest": item.get("material_representation_digest"),
                "ordinal": item.get("ordinal"),
            }
            for item in selected
        ],
    }
    projection["projection_hash"] = stable_hash(projection)
    return projection
