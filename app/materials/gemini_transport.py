from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..utils import utcnow


GEMINI_EXTERNAL_URL_REPRESENTATION = "gemini_external_url"
GEMINI_FILES_REPRESENTATION = "gemini_file_uri"
GEMINI_EXTERNAL_URL_ADAPTER_VERSION = "gemini-external-url-supabase/1"


@dataclass(frozen=True)
class GeminiTransportDecision:
    mode: str  # supabase_external_url | gemini_files
    total_bytes: int | None
    threshold_bytes: int
    source: str


class GeminiTransportPolicyError(ValueError):
    pass


def decide_gemini_transport(
    *,
    actual_size: int,
    request_file_total_bytes: int | None,
    request_file_count: int | None,
    threshold_bytes: int,
) -> GeminiTransportDecision:
    """Choose the Gemini material transport without changing the Gemini route.

    The 99 MiB rule is defined over the *sum of files in the current request*.
    A material call therefore accepts an aggregate byte hint. If the caller does
    not provide the aggregate, Relay only infers it when the caller explicitly
    says this is a single-file request. Otherwise Relay chooses Files API
    conservatively rather than risk treating a >99 MiB batch as External URL.
    """
    actual = int(actual_size)
    threshold = int(threshold_bytes)
    if actual < 0 or threshold <= 0:
        raise GeminiTransportPolicyError("invalid Gemini transport size configuration")

    total: int | None
    source: str
    if request_file_total_bytes is not None:
        total = int(request_file_total_bytes)
        if total < actual:
            raise GeminiTransportPolicyError(
                f"request_file_total_bytes cannot be smaller than this material: total={total} actual={actual}"
            )
        source = "caller_aggregate"
    elif request_file_count is not None and int(request_file_count) == 1:
        total = actual
        source = "single_file_inferred"
    else:
        # Missing aggregate information must never silently select the small-file
        # External URL path for a possibly-large multi-file request.
        total = None
        source = "aggregate_unknown_conservative"

    mode = "supabase_external_url" if total is not None and total <= threshold else "gemini_files"
    return GeminiTransportDecision(
        mode=mode,
        total_bytes=total,
        threshold_bytes=threshold,
        source=source,
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
