"""Deterministic cache admission. Selection priority never changes message Part order."""
from __future__ import annotations

from fractions import Fraction
from typing import Any

DEFAULT_INLINE_CACHE_BUDGET_BYTES = 60 * 1024 * 1024
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
URI_REFERENCE_WIRE_OVERHEAD_BYTES = 512

def effective_inline_budget_cost(size, binding, material_mode):
    from app.control_plane.cache_contracts import explicit_cache_material_mode
    mode=explicit_cache_material_mode({'explicit_cache_material_mode':material_mode})
    if mode=='file_uri_allowed' and (binding or {}).get('representation')=='gemini_file_uri':
        import json
        uri=binding.get('file_uri') or binding.get('external_uri')
        if not isinstance(uri,str) or not uri: raise ValueError('File URI representation has no URI')
        mime=(binding.get('metadata') or {}).get('mime_type') or binding.get('mime_type') or 'application/octet-stream'
        return max(URI_REFERENCE_WIRE_OVERHEAD_BYTES,len(json.dumps({'fileData':{'fileUri':uri,'mimeType':mime}},ensure_ascii=False).encode())+16)
    return size


def _priority(row: dict[str, Any], size: int) -> tuple[int, Fraction, int]:
    mime = str(row.get("content_type") or "").split(";")[0].lower()
    metadata = row.get("metadata") or {}
    if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        tier, estimate = 1, size // 4
    elif mime == "application/pdf":
        tier, estimate = (2 if size <= 15 * 1024 * 1024 else 3), size // 128
    elif mime == DOCX:
        tier, estimate = 3, size // 64
    elif mime.startswith("video/"):
        tier, estimate = 4, size // 4096
    else:
        tier, estimate = 5, min(4096, size // 256)
    projected_estimate = metadata.get("estimated_tokens") if isinstance(metadata, dict) else None
    if type(projected_estimate) is int and projected_estimate > 0:
        estimate = projected_estimate
    return tier, -Fraction(estimate, max(size, 1)), size


def select_materials_for_cache(
    rows: dict[str, dict[str, Any]], sizes: dict[str, int], ordered_ids: list[str], budget: int,
    *, explicit_cache_material_mode='inline_bytes_only', material_bindings=None,
) -> tuple[str, ...]:
    candidates = sorted(set(ordered_ids), key=lambda mid: (*_priority(rows[mid], sizes[mid]), mid))
    selected = set()
    for mid in candidates:
        cost=effective_inline_budget_cost(sizes[mid],(material_bindings or {}).get(mid),explicit_cache_material_mode)
        if cost <= budget:
            selected.add(mid)
            budget -= cost
    return tuple(mid for mid in dict.fromkeys(ordered_ids) if mid in selected)
