from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from ..utils import utcnow


GEMINI_EXTERNAL_URL_REPRESENTATION = "gemini_external_url"
GEMINI_FILES_REPRESENTATION = "gemini_file_uri"
GEMINI_EXTERNAL_URL_ADAPTER_VERSION = "gemini-external-url-supabase/1"


def project_gemini_input_content_type(source_content_type: str | None) -> str:
    """Project canonical Relay material MIME into Gemini input wire MIME.

    Relay keeps the source material content type unchanged in the material registry.
    Gemini input transport uses a provider-compatible MIME only at the wire/binding
    boundary. JSON artifacts are textual model inputs for Gemini and are projected
    to ``text/plain`` instead of being sent as ``fileData`` with
    ``application/json``.
    """
    source = str(source_content_type or "").strip()
    if not source:
        return "application/octet-stream"
    normalized = source.split(";", 1)[0].strip().lower()
    if normalized == "application/json":
        return "text/plain"
    return source


def project_gemini_external_url_filename(
    source_filename: str | None,
    source_content_type: str | None,
) -> str:
    """Project only the provider-facing External URL object filename.

    Canonical Relay material metadata is not changed.  When Gemini's input MIME
    projection changes a JSON artifact from ``application/json`` to
    ``text/plain``, the Supabase bridge object must expose a matching text-file
    name as well.  Other material types keep their original filename.
    """
    filename = str(source_filename or "material").strip() or "material"
    source = str(source_content_type or "").split(";", 1)[0].strip().lower()
    projected = project_gemini_input_content_type(source_content_type).split(";", 1)[0].strip().lower()
    if source == projected:
        return filename
    if source == "application/json" and projected == "text/plain":
        if filename.lower().endswith(".json"):
            return filename[:-5] + ".txt"
        return filename + ".txt"
    return filename


@dataclass(frozen=True)
class GeminiTransportDecision:
    mode: str  # gemini_files primary; supabase_external_url is fallback-only
    total_bytes: int
    threshold_bytes: int  # Relay 4.0 inline-cache fallback budget
    source: str
    caller_total_hint: int | None = None


class GeminiTransportPolicyError(ValueError):
    pass


def _mode(total_bytes: int, threshold_bytes: int) -> str:
    # Relay 4.0 no longer chooses Supabase by size. Files API is always primary.
    return "gemini_files"


def decide_gemini_transport(
    *,
    actual_size: int,
    request_file_total_bytes: int | None,
    request_file_count: int | None,
    threshold_bytes: int,
) -> GeminiTransportDecision:
    """Choose the primary Gemini ingress transport.

    Relay 4.0 always attempts Gemini Files API first. Aggregate hints remain
    diagnostic-only; ``threshold_bytes`` is the cache-inline fallback budget.
    Supabase External URL is created only after a Files API failure, and the 70 MiB cache fallback rule
    is applied later against the exact frozen Request material set.
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
        source="gemini_files_preferred",
        caller_total_hint=caller_hint,
    )


def decide_gemini_request_transport(
    *,
    material_sizes: Iterable[int],
    threshold_bytes: int,
) -> GeminiTransportDecision:
    """Return the Request aggregate for observability/legacy callers.

    The aggregate no longer changes the primary Gemini transport in Relay 4.0;
    Files API remains first choice for every material.
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
        source="relay_request_material_sum_files_preferred",
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
