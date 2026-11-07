"""Immutable, scoped native history fragments inside the existing history object."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .idempotency import canonical_json_bytes, stable_hash
from ..providers.base import ProviderRequestError

KEY = "_relay_frozen_transport"


def history_scope(snapshot: dict[str, Any]) -> str:
    return stable_hash([snapshot.get("session_id"), snapshot.get("connection_id"),
                        snapshot.get("route_binding_hash"), snapshot.get("protocol_profile_hash")])


def restore_history(history: list[dict[str, Any]], *, scope: str) -> list[dict[str, Any]]:
    result = copy.deepcopy(history)
    for turn in result:
        frozen = turn.get(KEY) if isinstance(turn, dict) else None
        if frozen is None:
            continue  # Pre-6.6 history is read unchanged, never silently re-authored.
        try:
            raw = frozen["native_json"].encode("utf-8")
            valid = (frozen["version"] == 1 and frozen["scope"] == scope
                     and hashlib.sha256(raw).hexdigest() == frozen["sha256"])
            native = json.loads(raw)
            valid = (valid and isinstance(native, dict) and native == turn.get("transport_history")
                     and canonical_json_bytes(native) == raw)
        except (KeyError, TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ProviderRequestError("CACHE_HISTORY_FROZEN_MISMATCH", "Frozen history identity or scope changed")
        turn["transport_history"] = native
    return result


def freeze_history_turn(turn: dict[str, Any], *, scope: str) -> dict[str, Any]:
    result = copy.deepcopy(turn)
    native = result.get("transport_history")
    if isinstance(native, dict):
        raw = canonical_json_bytes(native)
        result[KEY] = {"version": 1, "scope": scope, "native_json": raw.decode("utf-8"),
                       "sha256": hashlib.sha256(raw).hexdigest()}
    return result
