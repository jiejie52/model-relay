from __future__ import annotations

import copy
from typing import Any

from app.core.idempotency import stable_hash
from app.protocol_adapters.base import ProviderRequestError


class ContinuationSliceError(ProviderRequestError):
    """Raised when slicing continuation history fails with invalid or out-of-bounds parameters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def slice_continuation_history(
    history: list[dict[str, Any]] | None,
    covered_history_version: int | None,
    *,
    allow_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Safely slice history to retain only incremental turns beyond the covered version.

    Parameters:
        history: The list of history entries/turns.
        covered_history_version: The number of turns covered by the parent anchor (k).
            Delta turns are turns at index >= k.
        allow_fallback: If True (default), abnormal or out-of-bounds covered_history_version
            gracefully degrades by returning the full history. If False, a controlled
            ContinuationSliceError is raised.

    Returns:
        A list of sliced history turns (Multi-turn Delta).
    """
    if history is None:
        return []

    # If covered_history_version is None or 0, return complete history.
    if covered_history_version is None or covered_history_version == 0:
        return list(history)

    # Check for invalid types (e.g. bool, str, float)
    if not isinstance(covered_history_version, int) or isinstance(covered_history_version, bool):
        if allow_fallback:
            return list(history)
        raise ContinuationSliceError(
            "CONTINUATION_HISTORY_VERSION_INVALID",
            f"Invalid covered_history_version: {covered_history_version!r} (must be int)",
        )

    # Check negative version (k < 0)
    if covered_history_version < 0:
        if allow_fallback:
            return list(history)
        raise ContinuationSliceError(
            "CONTINUATION_HISTORY_VERSION_NEGATIVE",
            f"covered_history_version cannot be negative: {covered_history_version}",
        )

    # Check version exceeding history length (k > len(history))
    history_len = len(history)
    if covered_history_version > history_len:
        if allow_fallback:
            return list(history)
        raise ContinuationSliceError(
            "CONTINUATION_HISTORY_VERSION_EXCEEDED",
            f"covered_history_version ({covered_history_version}) exceeds history length ({history_len})",
        )

    # Normal valid slice: 0 <= covered_history_version <= history_len
    # When covered_history_version == history_len, history[history_len:] is []
    return list(history[covered_history_version:])


def build_continuation_delta_turns(
    history: list[dict[str, Any]],
    covered_history_version: int,
    current_input: Any,
    instructions: str | None = None,
    *,
    allow_fallback: bool = True,
) -> dict[str, Any]:
    """Assemble an incremental delta turns structure for wire adapters.

    Parameters:
        history: Canonical conversation history entries.
        covered_history_version: Parent anchor's covered version (k).
        current_input: Current turn payload (user query, contents, etc.).
        instructions: Optional system instructions for the session/request.
        allow_fallback: Whether slicing allows graceful fallback.

    Returns:
        A dict containing delta turns and metadata for adapter transmission.
    """
    total_count = len(history) if history is not None else 0
    delta_history = slice_continuation_history(
        history,
        covered_history_version,
        allow_fallback=allow_fallback,
    )
    is_delta = (
        isinstance(covered_history_version, int)
        and not isinstance(covered_history_version, bool)
        and 0 < covered_history_version <= total_count
        and len(delta_history) < total_count
    )

    delta_structure = {
        "schema_version": "relay-continuation-delta-turns/1",
        "covered_history_version": covered_history_version,
        "total_history_version": total_count,
        "delta_turns_count": len(delta_history),
        "delta_history": delta_history,
        "bound_delta_turns": delta_history,
        "current_input": current_input,
        "instructions": instructions,
        "is_delta": is_delta,
    }
    raw_for_hash = {
        "schema_version": delta_structure["schema_version"],
        "covered_history_version": covered_history_version,
        "delta_turns_count": len(delta_history),
        "delta_history": delta_history,
        "current_input": current_input,
        "instructions": instructions,
    }
    delta_structure["delta_hash"] = stable_hash(raw_for_hash)
    return delta_structure
