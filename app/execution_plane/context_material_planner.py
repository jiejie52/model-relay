"""Pure admission-time planner; resource liveness is verified by the runtime."""
from app.core.idempotency import stable_hash
from app.core.material_placement import (PLACEMENT_V2, EffectiveMaterialPlacementPlan,
    route_identity, verify_occurrences, MaterialPlacementError)


def placement_enabled(session, profile, mode):
    if (session.get("context_policy") != "conversation"
            or session.get("history_mode", "cumulative") == "ephemeral_turn"
            or profile.get("supports_native_history_material_reuse") is not True):
        return False
    marker = (session.get("metadata") or {}).get("_relay_material_placement") or {}
    return marker.get("contract_version") == PLACEMENT_V2 or (
        mode == "enforce_all_verified" and (int(session.get("history_version") or 0) == 0
            or bool(session.get("committed_material_ledger"))))


def plan_effective_materials(*, session, explicit_material_ids, history, profile, material_rows, deduplicate_auto=True, safety_seconds=120):
    kinds = list(profile.get("history_material_part_kinds") or [])
    if not kinds or not route_identity(session)["connection_spec_hash"] or not route_identity(session)["account_scope_id"]:
        raise MaterialPlacementError("MATERIAL_PLACEMENT_CONTRACT_INVALID")
    verified = verify_occurrences(session=session, history=history,
        ledger=session.get("committed_material_ledger") or [], kinds=kinds)
    by_id = {str(row["id"]): row for row in material_rows}
    for occ in verified:
        row = by_id.get(occ["material_id"])
        if not row or row.get("sha256") != occ["content_sha256"] or row.get("status") in {"deleted", "failed", "reupload_required"}:
            raise MaterialPlacementError("HISTORY_MATERIAL_OCCURRENCE_CORRUPTED")
    explicit = list(dict.fromkeys(explicit_material_ids))
    candidates = list(dict.fromkeys(session.get("material_manifest") or []))
    inherited = {o["material_id"] for o in verified}
    auto = [m for m in candidates if m not in explicit and (not deduplicate_auto or m not in inherited)]
    historical = [m for m in candidates if m not in explicit and deduplicate_auto and m in inherited]
    decisions = [{"material_id": m, "disposition": "inject_explicit" if m in explicit else
        "inherit_historical" if m in historical else "inject_auto"} for m in dict.fromkeys(candidates + explicit)]
    plan = {"contract_version": PLACEMENT_V2, "deduplicate_auto": deduplicate_auto, "binding_safety_seconds": safety_seconds, "identity": route_identity(session),
        "history_version": int(session.get("history_version") or 0),
        "manifest_hash": stable_hash(session.get("material_manifest") or []),
        "history_digest": stable_hash(history), "history_material_part_kinds": kinds,
        "auto_manifest_candidates": candidates, "current_explicit_material_ids": explicit,
        "verified_history_occurrences": verified, "historical_only_material_ids": historical,
        "current_auto_material_ids": auto, "current_effective_material_ids": auto + explicit,
        "decisions": decisions}
    plan["plan_digest"] = stable_hash(plan)
    return EffectiveMaterialPlacementPlan(plan)
