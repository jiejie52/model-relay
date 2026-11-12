"""Provider Binding lifecycle semantics shared by execution and runtime paths.

Provider adapters normally persist a usable Binding lifecycle state
(`active`, `ready`, `processed`) and may separately report their processing
state. `ready_provider` is retained as a legacy Binding compatibility value
because the previous historical validator explicitly accepted it; current
adapters do not emit it. An explicit processing state must also be complete;
Gemini's `UNSPECIFIED` is accepted only when the Binding lifecycle state
independently confirms usability.
"""
from __future__ import annotations

from typing import Any


PROVIDER_BINDING_USABLE_STATES = frozenset({"active", "ready", "ready_provider", "processed"})
PROVIDER_BINDING_UNREPORTED_PROCESSING_STATES = frozenset({
    "unspecified",
    "file_state_unspecified",
})


def normalize_provider_binding_state(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def provider_binding_state_failure_reason(binding: dict[str, Any] | None) -> str | None:
    """Return a stable failure reason, or ``None`` when the Binding is usable."""
    if not isinstance(binding, dict):
        return "BINDING_NOT_FOUND"

    lifecycle_state = normalize_provider_binding_state(binding.get("state"))
    processing_state = normalize_provider_binding_state(binding.get("processing_state"))
    effective_lifecycle_state = lifecycle_state or processing_state

    if effective_lifecycle_state not in PROVIDER_BINDING_USABLE_STATES:
        return "BINDING_STATE_INVALID"

    if (
        processing_state is not None
        and processing_state not in PROVIDER_BINDING_USABLE_STATES
        and processing_state not in PROVIDER_BINDING_UNREPORTED_PROCESSING_STATES
    ):
        return "BINDING_PROCESSING_NOT_READY"

    return None


def provider_binding_is_usable(binding: dict[str, Any] | None) -> bool:
    return provider_binding_state_failure_reason(binding) is None
