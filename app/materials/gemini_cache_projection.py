from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .gemini_transport import GEMINI_EXTERNAL_URL_REPRESENTATION, GEMINI_FILES_REPRESENTATION


GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES = 70 * 1024 * 1024
GEMINI_INLINE_CACHE_REPRESENTATION = "gemini_inline_data"
GEMINI_CACHE_PROJECTION_VERSION = "gemini-cache-material-projection/1"


@dataclass(frozen=True)
class GeminiCacheMaterialPlan:
    total_material_bytes: int
    inline_limit_bytes: int
    cache_material_ids: tuple[str, ...]
    inline_material_ids: tuple[str, ...]
    inference_only_session_material_ids: tuple[str, ...]
    files_api_material_ids: tuple[str, ...]
    external_fallback_material_ids: tuple[str, ...]
    mode: str

    def canonical(self) -> dict[str, Any]:
        return {
            "schema_version": "relay-gemini-cache-material-plan/1",
            "projection_version": GEMINI_CACHE_PROJECTION_VERSION,
            "mode": self.mode,
            "total_material_bytes": self.total_material_bytes,
            "inline_limit_bytes": self.inline_limit_bytes,
            "cache_material_ids": list(self.cache_material_ids),
            "inline_material_ids": list(self.inline_material_ids),
            "inference_only_session_material_ids": list(self.inference_only_session_material_ids),
            "files_api_material_ids": list(self.files_api_material_ids),
            "external_fallback_material_ids": list(self.external_fallback_material_ids),
        }


def plan_gemini_cache_materials(
    *,
    material_rows: Iterable[dict[str, Any]],
    material_bindings: Iterable[dict[str, Any]],
    session_material_ids: Iterable[str],
    inline_limit_bytes: int = GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES,
) -> GeminiCacheMaterialPlan:
    """Plan cache-vs-inference placement after Gemini Files API fallback.

    Gemini Files URI bindings are always eligible for CachedContent.  Only
    Session-stable materials whose Files upload fell back to External URL need
    the 70 MiB inline fallback rule.  For large aggregates, choose a stable
    subset by (size, material_id) and keep the cumulative raw bytes strictly
    below the configured limit.  Remaining Session materials stay inference-only.
    """

    rows = {str(row.get("id") or ""): row for row in material_rows if str(row.get("id") or "")}
    bindings = {
        str(binding.get("material_id") or ""): binding
        for binding in material_bindings
        if str(binding.get("material_id") or "")
    }
    session_ids = [str(mid) for mid in session_material_ids if str(mid)]
    limit = int(inline_limit_bytes)
    if limit <= 0:
        raise ValueError("Gemini inline cache fallback limit must be positive")

    def size_of(material_id: str) -> int:
        row = rows.get(material_id) or {}
        value = row.get("actual_size")
        if value is None:
            value = row.get("size_bytes")
        if value is None:
            raise ValueError(f"material size unavailable for {material_id}")
        size = int(value)
        if size < 0:
            raise ValueError(f"material size cannot be negative for {material_id}")
        return size

    total_bytes = sum(size_of(material_id) for material_id in rows)
    files_ids: list[str] = []
    external_ids: list[str] = []
    for material_id in session_ids:
        representation = str((bindings.get(material_id) or {}).get("representation") or "")
        if representation == GEMINI_FILES_REPRESENTATION:
            files_ids.append(material_id)
        elif representation == GEMINI_EXTERNAL_URL_REPRESENTATION:
            external_ids.append(material_id)

    if total_bytes < limit:
        inline_ids = list(external_ids)
        mode = "files_preferred_inline_all_fallbacks"
    else:
        inline_ids = []
        used = 0
        # Deterministic "small files first" selection. Strictly stay below 70 MiB.
        for material_id in sorted(external_ids, key=lambda mid: (size_of(mid), mid)):
            size = size_of(material_id)
            if used + size >= limit:
                continue
            inline_ids.append(material_id)
            used += size
        mode = "files_preferred_inline_small_subset"

    cache_set = set(files_ids) | set(inline_ids)
    cache_ids = [material_id for material_id in session_ids if material_id in cache_set]
    inference_only = [material_id for material_id in session_ids if material_id not in cache_set]

    return GeminiCacheMaterialPlan(
        total_material_bytes=total_bytes,
        inline_limit_bytes=limit,
        cache_material_ids=tuple(cache_ids),
        inline_material_ids=tuple(inline_ids),
        inference_only_session_material_ids=tuple(inference_only),
        files_api_material_ids=tuple(files_ids),
        external_fallback_material_ids=tuple(external_ids),
        mode=mode,
    )
