from __future__ import annotations

import hashlib
import json
from typing import Any


CANONICAL_REQUEST_VERSIONS = {
    "relay-request/2.2",
    "relay-request/2.3",
}
SUPPORTED_REQUEST_VERSIONS = {
    "relay-request/2.1",
    *CANONICAL_REQUEST_VERSIONS,
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def caller_intent_hash(value: dict[str, Any]) -> str:
    """Hash normalized public caller intent before cache/context planning.

    This is a fast replay discriminator, not the durable Request execution
    identity. It intentionally excludes current cache inventory, resource handles,
    wall-clock time and provider observations.
    """

    identity = {
        "input": value.get("input"),
        "instructions": value.get("instructions"),
        "material_ids": value.get("material_ids") or [],
        "provider": value.get("provider"),
        "model": value.get("model"),
        "think_level": value.get("think_level"),
        "execution": value.get("execution") or {},
        "structured_output": value.get("structured_output") or {},
        "options": value.get("options") or {},
        # provider_payload remains a public migration input. Treat changes as
        # caller-intent changes even though v3 only accepts aliases that Relay
        # can normalize into canonical options.
        "provider_payload": value.get("provider_payload") or {},
        "requested_cache_mode": value.get("requested_cache_mode"),
        "metadata": value.get("metadata") or {},
    }
    return stable_hash(identity)


def request_identity(snapshot: dict[str, Any]) -> str:
    """Hash execution-relevant facts using the snapshot's exact contract version.

    2.1 and 2.2 retain their historical identity algorithms so persisted Requests
    remain resumable. 2.3 adds cache/context and the complete frozen execution
    contract identities. Unknown versions fail closed rather than silently falling
    into provider_payload compatibility.
    """

    version = str(snapshot.get("schema_version") or "relay-request/2.1")
    if version not in SUPPORTED_REQUEST_VERSIONS:
        raise ValueError(f"Unsupported Relay Request identity version: {version}")

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

    if version == "relay-request/2.1":
        identity["provider_payload"] = snapshot.get("provider_payload") or {}
        return stable_hash(identity)

    identity.update(
        {
            "options": snapshot.get("options") or {},
            "effective_options": snapshot.get("effective_options") or {},
            "capability_revision": snapshot.get("capability_revision"),
        }
    )
    if version == "relay-request/2.2":
        return stable_hash(identity)

    # relay-request/2.3: freeze the cache-aware execution contract. Physical
    # cache resource handles/generations are deliberately excluded and live in
    # the later CacheExecutionBinding.
    identity.update(
        {
            "requested_think_level": snapshot.get("requested_think_level"),
            "structured_output_guarantee": snapshot.get("structured_output_guarantee"),
            "offering_id": snapshot.get("offering_id"),
            "channel_id": snapshot.get("channel_id"),
            "protocol": snapshot.get("protocol"),
            "control_plane_hash": snapshot.get("control_plane_hash"),
            "route_revision": snapshot.get("route_revision"),
            "route_binding_hash": snapshot.get("route_binding_hash"),
            "capability_contract_id": snapshot.get("capability_contract_id"),
            "capability_contract_hash": snapshot.get("capability_contract_hash"),
            "protocol_profile_id": snapshot.get("protocol_profile_id"),
            "protocol_profile_hash": snapshot.get("protocol_profile_hash"),
            "cache_policy_id": snapshot.get("cache_policy_id"),
            "cache_policy_hash": snapshot.get("cache_policy_hash"),
            "cache_contract_hash": snapshot.get("cache_contract_hash"),
            "context_policy": snapshot.get("context_policy"),
            "history_version": snapshot.get("history_version"),
            "history_object_hash": snapshot.get("history_object_hash"),
            "effective_material_ids": snapshot.get("effective_material_ids") or [],
            "material_total_bytes": snapshot.get("material_total_bytes"),
            "requested_cache_mode": snapshot.get("requested_cache_mode"),
            "cache_plan_hash": snapshot.get("cache_plan_hash"),
            "context_plan_hash": snapshot.get("context_plan_hash"),
        }
    )
    return stable_hash(identity)
