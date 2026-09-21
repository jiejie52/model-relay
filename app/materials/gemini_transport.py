from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from ..utils import utcnow


GEMINI_EXTERNAL_URL_REPRESENTATION = "gemini_external_url"
GEMINI_FILES_REPRESENTATION = "gemini_file_uri"
GEMINI_EXTERNAL_URL_ADAPTER_VERSION = "gemini-external-url-supabase/1"


@dataclass(frozen=True)
class GeminiTransportDecision:
    mode: str  # supabase_external_url | gemini_files
    total_bytes: int
    threshold_bytes: int
    source: str
    caller_total_hint: int | None = None


class GeminiTransportPolicyError(ValueError):
    pass


def _mode(total_bytes: int, threshold_bytes: int) -> str:
    return "supabase_external_url" if total_bytes <= threshold_bytes else "gemini_files"


def decide_gemini_transport(
    *,
    actual_size: int,
    request_file_total_bytes: int | None,
    request_file_count: int | None,
    threshold_bytes: int,
) -> GeminiTransportDecision:
    """Choose ingress transport from bytes Relay actually received.

    0.5.3 makes Relay authoritative for size. ``request_file_total_bytes`` and
    ``request_file_count`` are retained only as legacy diagnostics; they never
    select a transport. At single-material ingress time Relay can prove the
    current material size, so it uses that size immediately. The final
    multi-material request aggregate is recalculated by ``BindingResolver``
    before provider dispatch and can promote External-URL materials to Files API
    if the complete Request exceeds the threshold.
    """
    actual = int(actual_size)
    threshold = int(threshold_bytes)
    if actual < 0 or threshold <= 0:
        raise GeminiTransportPolicyError("invalid Gemini transport size configuration")

    caller_hint = None if request_file_total_bytes is None else int(request_file_total_bytes)
    if caller_hint is not None and caller_hint < 0:
        raise GeminiTransportPolicyError("request_file_total_bytes must be >= 0")
    if request_file_count is not None and int(request_file_count) < 1:
        raise GeminiTransportPolicyError("request_file_count must be >= 1")

    return GeminiTransportDecision(
        mode=_mode(actual, threshold),
        total_bytes=actual,
        threshold_bytes=threshold,
        source="relay_actual_bytes",
        caller_total_hint=caller_hint,
    )


def decide_gemini_request_transport(
    *,
    material_sizes: Iterable[int],
    threshold_bytes: int,
) -> GeminiTransportDecision:
    """Authoritatively select Gemini transport for the complete Request.

    The caller does not provide aggregate bytes. Relay sums ``actual_size`` from
    the material registry for the exact frozen Request material set.
    """
    threshold = int(threshold_bytes)
    if threshold <= 0:
        raise GeminiTransportPolicyError("invalid Gemini transport size configuration")
    sizes = [int(x) for x in material_sizes]
    if any(x < 0 for x in sizes):
        raise GeminiTransportPolicyError("material size cannot be negative")
    total = sum(sizes)
    return GeminiTransportDecision(
        mode=_mode(total, threshold),
        total_bytes=total,
        threshold_bytes=threshold,
        source="relay_request_material_sum",
    )


def external_url_binding(
    *,
    material_id: str,
    connection_id: str,
    account_scope_hash: str,
    external_url: str,
    object_id: str,
    generation: int,
    ttl_seconds: int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expires_at = utcnow() + timedelta(seconds=int(ttl_seconds))
    return {
        "material_id": material_id,
        "provider": "gemini",
        "connection_id": connection_id,
        "account_scope_hash": account_scope_hash,
        "purpose": "file",
        "representation": GEMINI_EXTERNAL_URL_REPRESENTATION,
        "adapter_version": GEMINI_EXTERNAL_URL_ADAPTER_VERSION,
        "external_file_id": f"supabase:{object_id}",
        "external_uri": external_url,
        "provider_file_id": f"supabase:{object_id}",
        "file_uri": external_url,
        "state": "active",
        "processing_state": "active",
        "generation": int(generation),
        "expires_at": expires_at.isoformat(),
        "last_verified_at": utcnow().isoformat(),
        "metadata": dict(metadata or {}),
    }
