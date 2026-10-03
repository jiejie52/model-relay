from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .gemini_transport import GEMINI_EXTERNAL_URL_REPRESENTATION, GEMINI_FILES_REPRESENTATION


GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES = 70 * 1024 * 1024
GEMINI_INLINE_CACHE_REPRESENTATION = "gemini_inline_data"
GEMINI_CACHE_PROJECTION_VERSION = "gemini-cache-material-projection/2"
GEMINI_CACHE_PROJECTION_VERSION_LEGACY = "gemini-cache-material-projection/1"


@dataclass(frozen=True)
class GeminiCacheMaterialPlan:
    total_material_bytes: int
    inline_limit_bytes: int
    cache_material_ids: tuple[str, ...]
    inline_material_ids: tuple[str, ...]
    inference_only_session_material_ids: tuple[str, ...]
    files_api_inference_material_ids: tuple[str, ...]
    external_url_inference_material_ids: tuple[str, ...]
    mode: str

    def canonical(self) -> dict[str, Any]:
        return {
            "schema_version": "relay-gemini-cache-material-plan/2",
            "projection_version": GEMINI_CACHE_PROJECTION_VERSION,
            "mode": self.mode,
            "total_material_bytes": self.total_material_bytes,
            "inline_limit_bytes": self.inline_limit_bytes,
            "cache_material_ids": list(self.cache_material_ids),
            "inline_material_ids": list(self.inline_material_ids),
            "inference_only_session_material_ids": list(self.inference_only_session_material_ids),
            "files_api_inference_material_ids": list(self.files_api_inference_material_ids),
            "external_url_inference_material_ids": list(self.external_url_inference_material_ids),
            # New layout deliberately never uses Gemini File objects inside
            # CachedContent. Keep this explicit for diagnostics.
            "files_api_cache_material_ids": [],
        }


def _rows_and_sizes(material_rows: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    rows = {str(row.get("id") or ""): row for row in material_rows if str(row.get("id") or "")}
    sizes: dict[str, int] = {}
    for material_id, row in rows.items():
        value = row.get("actual_size")
        if value is None:
            value = row.get("size_bytes")
        if value is None:
            raise ValueError(f"material size unavailable for {material_id}")
        size = int(value)
        if size < 0:
            raise ValueError(f"material size cannot be negative for {material_id}")
        sizes[material_id] = size
    return rows, sizes


def select_gemini_inline_cache_material_ids(
    *,
    material_rows: Iterable[dict[str, Any]],
    session_material_ids: Iterable[str],
    inline_limit_bytes: int = GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES,
) -> tuple[str, ...]:
    """Select the stable material subset that is injected into CachedContent.

    Under the aggregate limit, every stable Session material is selected. At or
    above the limit, select small files deterministically by ``(size, id)`` and
    keep the cumulative raw-byte total strictly below the configured budget.
    Request-only material is never promoted into the stable cache prefix.
    """

    rows, sizes = _rows_and_sizes(material_rows)
    limit = int(inline_limit_bytes)
    if limit <= 0:
        raise ValueError("Gemini inline cache limit must be positive")
    session_ids = [str(mid) for mid in session_material_ids if str(mid) and str(mid) in rows]
    total_bytes = sum(sizes.values())
    if total_bytes < limit:
        return tuple(session_ids)

    selected: set[str] = set()
    used = 0
    for material_id in sorted(session_ids, key=lambda mid: (sizes[mid], mid)):
        size = sizes[material_id]
        if used + size >= limit:
            continue
        selected.add(material_id)
        used += size
    return tuple(material_id for material_id in session_ids if material_id in selected)


def plan_gemini_cache_materials(
    *,
    material_rows: Iterable[dict[str, Any]],
    material_bindings: Iterable[dict[str, Any]],
    session_material_ids: Iterable[str],
    inline_limit_bytes: int = GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES,
) -> GeminiCacheMaterialPlan:
    """Plan Relay 4.3 Gemini inline cache + inference placement.

    CachedContent receives only Relay-owned inline bytes. Gemini Files API is
    never referenced from CachedContent. When the Request aggregate is below
    70 MiB, Gemini Files API is not used at all. At/above 70 MiB, stable small
    files are cached inline and every remaining material is an inference input,
    with Gemini Files API as its preferred transport (External URL on failure).
    """

    rows, sizes = _rows_and_sizes(material_rows)
    bindings = {
        str(binding.get("material_id") or ""): binding
        for binding in material_bindings
        if str(binding.get("material_id") or "")
    }
    session_ids = [str(mid) for mid in session_material_ids if str(mid) and str(mid) in rows]
    limit = int(inline_limit_bytes)
    if limit <= 0:
        raise ValueError("Gemini inline cache limit must be positive")

    total_bytes = sum(sizes.values())
    inline_ids = list(
        select_gemini_inline_cache_material_ids(
            material_rows=rows.values(),
            session_material_ids=session_ids,
            inline_limit_bytes=limit,
        )
    )
    inline_set = set(inline_ids)
    inference_only = [mid for mid in session_ids if mid not in inline_set]

    all_ids = list(rows.keys())
    if total_bytes < limit:
        files_inference: list[str] = []
        external_inference = [mid for mid in all_ids if mid not in inline_set]
        mode = "inline_cache_all_no_files"
    else:
        files_inference = [mid for mid in all_ids if mid not in inline_set]
        # The frozen binding may be External URL after a Files upload failure.
        external_inference = [
            mid
            for mid in files_inference
            if str((bindings.get(mid) or {}).get("representation") or "") == GEMINI_EXTERNAL_URL_REPRESENTATION
        ]
        mode = "inline_cache_small_subset_files_for_remaining"

    return GeminiCacheMaterialPlan(
        total_material_bytes=total_bytes,
        inline_limit_bytes=limit,
        cache_material_ids=tuple(inline_ids),
        inline_material_ids=tuple(inline_ids),
        inference_only_session_material_ids=tuple(inference_only),
        files_api_inference_material_ids=tuple(files_inference),
        external_url_inference_material_ids=tuple(external_inference),
        mode=mode,
    )


def plan_gemini_cache_materials_legacy(
    *,
    material_rows: Iterable[dict[str, Any]],
    material_bindings: Iterable[dict[str, Any]],
    session_material_ids: Iterable[str],
    inline_limit_bytes: int = GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES,
) -> dict[str, Any]:
    """Preserve layout/3-5 cache projection for already-frozen Sessions."""

    rows, sizes = _rows_and_sizes(material_rows)
    bindings = {
        str(binding.get("material_id") or ""): binding
        for binding in material_bindings
        if str(binding.get("material_id") or "")
    }
    session_ids = [str(mid) for mid in session_material_ids if str(mid)]
    limit = int(inline_limit_bytes)
    total_bytes = sum(sizes.values())
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
        for material_id in sorted(external_ids, key=lambda mid: (sizes[mid], mid)):
            size = sizes[material_id]
            if used + size >= limit:
                continue
            inline_ids.append(material_id)
            used += size
        mode = "files_preferred_inline_small_subset"

    cache_set = set(files_ids) | set(inline_ids)
    cache_ids = [mid for mid in session_ids if mid in cache_set]
    inference_only = [mid for mid in session_ids if mid not in cache_set]
    return {
        "schema_version": "relay-gemini-cache-material-plan/1",
        "projection_version": GEMINI_CACHE_PROJECTION_VERSION_LEGACY,
        "mode": mode,
        "total_material_bytes": total_bytes,
        "inline_limit_bytes": limit,
        "cache_material_ids": cache_ids,
        "inline_material_ids": inline_ids,
        "inference_only_session_material_ids": inference_only,
        "files_api_material_ids": files_ids,
        "external_fallback_material_ids": external_ids,
    }
