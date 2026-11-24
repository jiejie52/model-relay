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


def transport_user_parts(transport: dict[str, Any] | None) -> tuple[list[dict[str, Any]], bool]:
    """Return user content part list and whether transport uses Responses layout."""
    if not isinstance(transport, dict):
        return [], False
    if transport.get("kind") == "responses":
        user_item = transport.get("user_item")
        content = user_item.get("content") if isinstance(user_item, dict) else None
        return (list(content) if isinstance(content, (list, tuple)) else []), True
    user_content = transport.get("user_content")
    parts = user_content.get("parts") if isinstance(user_content, dict) else None
    return (list(parts) if isinstance(parts, (list, tuple)) else []), False


def is_material_part(
    part: Any,
    kinds: list[str] | tuple[str, ...],
    *,
    is_responses: bool = False,
    index: int = 0,
    total_parts: int = 0,
) -> bool:
    """Return whether a native user content part represents an attached Material."""
    if not isinstance(part, dict):
        return False
    if is_responses:
        ptype = part.get("type")
        if ptype not in kinds:
            return False
        if ptype in {"input_file", "input_image"}:
            return True
        if ptype == "input_text":
            # In material-placement/2 Responses layout, material items precede the trailing user prompt.
            return index < max(0, total_parts - 1)
        return False
    return any(k in part for k in kinds)


def checked_history(history, kinds, transport_kind):
    """Validate native history before it reaches any dispatch boundary."""
    if not isinstance(history, (list, tuple)):
        raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
    for turn in history:
        transport = turn.get("transport_history") if isinstance(turn, dict) else None
        if not isinstance(transport, dict) or transport.get("kind") != transport_kind:
            raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
        if transport_kind == "gemini_interactions":
            u = transport.get("interaction_user_input") or transport.get("user_content")
            m = transport.get("interaction_model_steps") or transport.get("steps") or transport.get("model_content")
            if not u or not m:
                raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
        elif transport_kind == "responses":
            u = transport.get("user_item")
            m = transport.get("response_output")
            if (not isinstance(u, dict) or u.get("role") != "user"
                    or not isinstance(u.get("content"), (list, tuple)) or not u["content"]
                    or any(not isinstance(p, dict) or not p for p in u["content"])
                    or not isinstance(m, (list, tuple))):
                raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
        else:
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
            parts, is_resp = transport_user_parts(history[index]["transport_history"])
            part = parts[part_index]
            if not is_material_part(part, kinds, is_responses=is_resp, index=part_index, total_parts=len(parts)) or stable_hash(part) != occ["part_digest"]:
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
    expected = set()
    for i, turn in enumerate(history):
        parts, is_resp = transport_user_parts(turn["transport_history"])
        total = len(parts)
        for j, part in enumerate(parts):
            if is_material_part(part, kinds, is_responses=is_resp, index=j, total_parts=total):
                expected.add((i, j))
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
    context_policy = plan.get("context_policy", "conversation")
    if context_policy not in {"conversation", "explicit", "session_materials_only"}:
        raise MaterialPlacementError("MATERIAL_PLACEMENT_CONTRACT_INVALID")
    candidates = (list(dict.fromkeys(session.get("material_manifest") or []))
        if context_policy != "explicit" else [])
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


def verify_parent_chain_material_coverage(
    *,
    placement_plan: dict[str, Any],
    anchor_material_bindings: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    delta_turns: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    """Verify that every skipped historical_only material is covered by the parent anchor or delta turns."""
    historical_only = set(placement_plan.get("historical_only_material_ids") or [])
    if not historical_only:
        return True, None

    covered_mids: set[str] = {
        str(b["material_id"])
        for b in (anchor_material_bindings or ())
        if isinstance(b, dict) and b.get("material_id")
    }
    for turn in delta_turns or ():
        if not isinstance(turn, dict):
            continue
        for occ in (turn.get("material_occurrences") or ()):
            if isinstance(occ, dict) and occ.get("material_id"):
                covered_mids.add(str(occ["material_id"]))
        for mid in (turn.get("material_ids") or ()):
            if mid:
                covered_mids.add(str(mid))

    missing = sorted(historical_only - covered_mids)
    if missing:
        return False, "material_coverage_gap"
    return True, None
