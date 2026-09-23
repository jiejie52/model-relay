from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def request_identity(snapshot: dict[str, Any]) -> str:
    """Hash every execution-relevant field frozen at acceptance time.

    relay-request/2.2 hashes canonical requested/effective options plus the
    capability profile revision. Older snapshots retain provider_payload in the
    identity so in-flight 2.1 Requests remain resumable after a Relay upgrade.
    """

    identity = {
        "session_id": snapshot.get("session_id"),
        "owner": {
            "tenant_id": snapshot.get("tenant_id"),
            "conversation_hash": snapshot.get("conversation_hash"),
        },
        "input": snapshot.get("input"),
        "instructions": snapshot.get("instructions"),
        "material_ids": snapshot.get("material_ids") or [],
        "material_hashes": snapshot.get("material_hashes") or {},
        "provider": snapshot.get("provider"),
        "connection_id": snapshot.get("connection_id"),
        "model": snapshot.get("model"),
        "think_level": snapshot.get("think_level"),
        "structured_output": snapshot.get("structured_output") or {},
        "execution": snapshot.get("execution") or {},
    }
    if str(snapshot.get("schema_version") or "") == "relay-request/2.2":
        identity.update(
            {
                "options": snapshot.get("options") or {},
                "effective_options": snapshot.get("effective_options") or {},
                "capability_revision": snapshot.get("capability_revision"),
            }
        )
    else:
        identity["provider_payload"] = snapshot.get("provider_payload") or {}
    return stable_hash(identity)
