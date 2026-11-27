"""Static supply qualification without startup upstream network probes.

Model capabilities, continuation eligibility, and protocol constraints are
established during initial onboarding and compiled into immutable Excel/control-plane
contracts. Startup qualification performs local credential and cross-contract
checks only, with zero upstream HTTP calls.
"""
from __future__ import annotations

from app.control_plane.upstream_retention import resolve_upstream_retention

VALIDATOR_VERSION = "static-contract/10.10.0"
GEMINI_INTERACTIONS_VALIDATOR_VERSION = "static-contract/gemini-interactions/10.10.0"


def validation_version(protocol: str) -> str:
    return GEMINI_INTERACTIONS_VALIDATOR_VERSION if protocol == "gemini_interactions" else VALIDATOR_VERSION


class ProbeFailure(RuntimeError):
    pass


def qualify_supply_static(plane, settings, supply) -> dict:
    """Qualify an enabled supply purely from local credentials and compiled contracts."""
    spec = plane.connection(supply.connection_id)
    cap = plane.contract(supply.capability_contract_id)
    secret = plane.credential(supply.connection_id, settings)
    if not secret:
        return {"status": "pending", "reason": "credential_missing"}

    cont = cap.continuation if isinstance(cap.continuation, dict) else {}
    cont_policy = (
        plane.continuation_policy(supply.continuation_policy_id)
        if getattr(supply, "continuation_policy_id", None)
        else None
    )
    cont_anchor_class = str(cont.get("anchor_class") or "none")
    policy = cont_policy.canonical() if cont_policy is not None else {}
    retention = resolve_upstream_retention(spec.metadata, cap.canonical(), policy)

    stateful_enabled = (
        retention["allow_store"]
        and cont_anchor_class in {"immutable_tree_anchor", "linear_ephemeral_cursor", "mutable_session_container"}
        and cont.get("requires_upstream_store") is True
        and policy.get("default_mode") in {"prefer_stateful_anchor", "require_stateful_anchor"}
    )
    continuation_reason = None if stateful_enabled else (
        retention["reason"] if not retention["allow_store"] else "continuation_not_enabled"
    )
    if policy.get("default_mode") == "require_stateful_anchor" and not stateful_enabled:
        return {"status": "pending", "reason": continuation_reason or "continuation_not_enabled"}

    protocol = spec.protocol
    modes = cap.metadata.get("material_transport", {}).get("supported_material_transports", [])
    mechanisms = cap.cache.get("supported_mechanisms", [])

    facets: list[str] = ["inference", "representative_thinking", "structured_output"]
    if "provider_files_api" in modes:
        facets.append("provider_files_api")
    if protocol in {"gemini_interactions", "gemini_native"}:
        facets.append("relay_post_validate")
        if protocol == "gemini_interactions":
            facets.append("relay_result_object")
    for mech in mechanisms:
        if mech not in facets:
            facets.append(mech)
    if stateful_enabled:
        facets.append("stateful_continuation")
        if cont.get("supports_multiturn_delta"):
            facets.append("multiturn_delta")
        if cont.get("supports_branching"):
            facets.append("branching")

    return {
        "status": "ready",
        "facets": facets,
        "validator_version": validation_version(protocol),
        "continuation_reason": continuation_reason,
    }


async def verify_supply(settings, plane, supply, *, journal=None):
    """Compatibility wrapper executing static local qualification without network I/O."""
    return qualify_supply_static(plane, settings, supply)
