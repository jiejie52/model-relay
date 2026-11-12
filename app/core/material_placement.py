"""Immutable, provider-neutral material placement contract shared by layers.

This module performs no I/O and never changes a frozen history or binding.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from app.immutable import freeze_json, thaw_json
from app.core.idempotency import stable_hash

PLACEMENT_V2 = "material-placement/2"


class MaterialPlacementError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CommittedMaterialOccurrence:
    facts: dict[str, Any]

    def __post_init__(self):
        object.__setattr__(self, "facts", freeze_json(self.facts))

    def canonical(self):
        return thaw_json(self.facts)


@dataclass(frozen=True)
class EffectiveMaterialPlacementPlan:
    facts: dict[str, Any]

    def __post_init__(self):
        object.__setattr__(self, "facts", freeze_json(self.facts))

    def canonical(self):
        return thaw_json(self.facts)

    def verify_integrity(self, *, session, history):
        return verify_placement(self.canonical(), session=session, history=history)


def owner_scope(session):
    return stable_hash({k: str(session[k]) for k in ("tenant_id", "conversation_hash")})


def route_identity(session):
    route = (session.get("metadata") or {}).get("_relay_route") or {}
    return {"session_id": str(session["id"]), "tenant_scope_hash": owner_scope(session),
            "connection_id": str(session.get("connection_id") or ""),
            "account_scope_id": str(route.get("account_scope_id") or ""),
            "connection_spec_hash": str(route.get("connection_spec_hash") or "")}


def checked_history(history, kinds, transport_kind):
    """Validate native history before it reaches any dispatch boundary."""
    if not isinstance(history, (list, tuple)):
        raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
    for turn in history:
        transport = turn.get("transport_history") if isinstance(turn, dict) else None
        if not isinstance(transport, dict) or transport.get("kind") != transport_kind:
            raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
        for field, role in (("user_content", "user"), ("model_content", "model")):
            content = transport.get(field)
            if (not isinstance(content, dict) or content.get("role") != role
                    or not isinstance(content.get("parts"), (list, tuple)) or not content["parts"]):
                raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
            if any(not isinstance(p, dict) or not p for p in content["parts"]):
                raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
        prior = turn.get("_relay_context_freeze")
        if prior and prior.get("transport_hash") != stable_hash(transport):
            raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
    return history


def verify_occurrences(*, session, history, ledger, kinds):
    profile = (session.get("metadata") or {}).get("_relay_route", {}).get("protocol_profile") or {}
    checked_history(history, kinds, profile.get("protocol"))
    if len(history) != int(session.get("history_version") or 0) or not isinstance(ledger, (list, tuple)):
        raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
    identity = route_identity(session)
    verified, occupied = [], set()
    flattened = [o for turn in history for o in (turn.get("material_occurrences") or [])]
    if list(ledger) != flattened:
        raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
    for occ in ledger:
        try:
            index, part_index = occ["history_entry_index"], occ["part_index"]
            if (type(index) is not int or type(part_index) is not int or index < 0 or part_index < 0
                    or any(occ.get(k) != v for k, v in identity.items())
                    or occ["history_version"] != index + 1 or occ["user_content_index"] != 2 * index
                    or occ["source_request_id"] != str(history[index]["request_id"])
                    or occ["transport_history_hash"] != stable_hash(history[index]["transport_history"])
                    or occ["origin"] not in {"session_manifest_auto", "request_explicit"}
                    or not occ["material_id"] or not occ["content_sha256"]
                    or type(occ["binding_generation"]) is not int or occ["binding_generation"] <= 0):
                raise ValueError("identity")
            binding = occ["binding"]
            if (binding.get("material_id") != occ["material_id"]
                    or binding.get("content_sha256") != occ["content_sha256"]
                    or binding.get("binding_generation") != occ["binding_generation"]
                    or binding.get("representation") != occ["representation_kind"]
                    or binding.get("connection_id") != identity["connection_id"]
                    or binding.get("account_scope_hash") != identity["account_scope_id"]):
                raise ValueError("binding identity")
            part = history[index]["transport_history"]["user_content"]["parts"][part_index]
            if not any(k in part for k in kinds) or stable_hash(part) != occ["part_digest"]:
                raise ValueError("part")
            position = (index, part_index)
            if position in occupied:
                raise ValueError("duplicate position")
            occupied.add(position)
            verified.append(dict(occ))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED") from exc
    # All user attachment occurrences must be accounted for, including prior
    # explicit re-submissions and materials outside the Session manifest.
    expected = {(i, j) for i, turn in enumerate(history)
                for j, part in enumerate(turn["transport_history"]["user_content"]["parts"])
                if any(k in part for k in kinds)}
    if occupied != expected:
        raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
    return verified


def verify_placement(plan, *, session, history):
    if not isinstance(plan, dict) or plan.get("contract_version") != PLACEMENT_V2:
        raise MaterialPlacementError("MATERIAL_PLACEMENT_CONTRACT_INVALID")
    profile = (session.get("metadata") or {}).get("_relay_route", {}).get("protocol_profile") or {}
    if (profile.get("supports_native_history_material_reuse") is not True
            or plan.get("history_material_part_kinds") != profile.get("history_material_part_kinds")):
        raise MaterialPlacementError("MATERIAL_PLACEMENT_CONTRACT_INVALID")
    if plan.get("plan_digest") != stable_hash({k: v for k, v in plan.items() if k != "plan_digest"}):
        raise MaterialPlacementError("MATERIAL_PLACEMENT_DIGEST_MISMATCH")
    if (plan.get("identity") != route_identity(session)
            or plan.get("history_version") != int(session.get("history_version") or 0)
            or plan.get("manifest_hash") != stable_hash(session.get("material_manifest") or [])
            or plan.get("history_digest") != stable_hash(history)):
        raise MaterialPlacementError("MATERIAL_PLACEMENT_CONTEXT_MISMATCH")
    verified = verify_occurrences(session=session, history=history,
        ledger=session.get("committed_material_ledger") or [], kinds=plan["history_material_part_kinds"])
    if verified != plan.get("verified_history_occurrences"):
        raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
    if (type(plan.get("binding_safety_seconds")) is not int
            or not 30 <= plan["binding_safety_seconds"] <= 3600):
        raise MaterialPlacementError("MATERIAL_PLACEMENT_CONTRACT_INVALID")
    explicit = plan["current_explicit_material_ids"]
    inherited = {o["material_id"] for o in verified}
    candidates = list(dict.fromkeys(session.get("material_manifest") or []))
    deduplicate = plan.get("deduplicate_auto", True)
    if type(deduplicate) is not bool:
        raise MaterialPlacementError("MATERIAL_PLACEMENT_CONTRACT_INVALID")
    auto = [m for m in candidates if m not in explicit and (not deduplicate or m not in inherited)]
    historical = [m for m in candidates if m not in explicit and deduplicate and m in inherited]
    if (plan["auto_manifest_candidates"] != candidates or plan["current_auto_material_ids"] != auto
            or plan["historical_only_material_ids"] != historical
            or explicit != list(dict.fromkeys(explicit))
            or plan["current_effective_material_ids"] != auto + explicit):
        raise MaterialPlacementError("MATERIAL_PLACEMENT_DECISION_MISMATCH")
    return plan


def placement_summary(plan):
    return {"placement_contract_version": plan["contract_version"], "plan_digest": plan["plan_digest"],
        "auto_manifest_count": len(plan["auto_manifest_candidates"]),
        "verified_history_material_count": len({o["material_id"] for o in plan["verified_history_occurrences"]}),
        "auto_materials_skipped": len(plan["historical_only_material_ids"]),
        "current_explicit_count": len(plan["current_explicit_material_ids"]),
        "current_bound_material_count": len(plan["current_effective_material_ids"])}

def material_identity(snapshot):
    if snapshot.get("placement_contract_version") != PLACEMENT_V2:
        raise ValueError("Unknown material placement identity version")
    plan = snapshot.get("material_placement_plan") or {}
    if plan.get("contract_version") != PLACEMENT_V2:
        raise ValueError("Missing frozen material placement plan")
    return {"placement_contract_version": PLACEMENT_V2,
        "expected_history_version": snapshot.get("history_version"),
        "session_manifest_hash": plan["manifest_hash"],
        "explicit_material_ids": snapshot.get("material_ids") or []}
