"""Verify resources referenced by immutable history without rebinding them."""
from datetime import datetime, timedelta, timezone
import logging

from app.core.idempotency import stable_hash
from app.core.material_placement import verify_placement, route_identity
from app.core.provider_binding_state import provider_binding_state_failure_reason
from app.observability import error as log_error
from app.protocol_adapters.base import ProviderRequestError
from app.utils import utcnow


logger = logging.getLogger("model-relay-material-bindings")


def _generation(value, *, default=None):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _remaining_ttl_seconds(expires_at):
    if not expires_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int((parsed - utcnow()).total_seconds())
    except (TypeError, ValueError):
        return None


def _log_binding_validation_failure(state, snapshot, row, reason, *, historical, stage=None):
    identity = route_identity(state.session)
    log_error(
        logger,
        "provider_binding_validation_failed",
        request_id=state.request_id,
        session_id=state.session_id,
        material_id=snapshot.get("material_id"),
        connection_id=snapshot.get("connection_id") or identity.get("connection_id"),
        expected_generation=_generation(snapshot.get("binding_generation")),
        actual_generation=_generation(row.get("generation"), default=1) if row else None,
        binding_state=row.get("state") if row else None,
        processing_state=row.get("processing_state") if row else None,
        validation_failure_reason=reason,
        remaining_ttl_seconds=_remaining_ttl_seconds(row.get("expires_at")) if row else None,
        failure_stage=stage or (
            "runtime_historical_binding_validation" if historical
            else "runtime_current_turn_binding_validation"
        ),
        historical=historical,
        failure_class="relay_validation",
    )


async def checked_binding(repo, snapshot, state, *, historical):
    code = "HISTORICAL_MATERIAL_BINDING_EXPIRED" if historical else "MATERIAL_BINDING_INVALID"
    identity = route_identity(state.session)
    if (snapshot.get("connection_id") != identity["connection_id"]
            or snapshot.get("account_scope_hash") != identity["account_scope_id"]):
        _log_binding_validation_failure(
            state, snapshot, None, "BINDING_SCOPE_MISMATCH", historical=historical,
            stage="runtime_frozen_scope_validation",
        )
        raise ProviderRequestError(code, "Material binding differs from frozen execution scope")

    material = await repo.get_material(
        snapshot["material_id"],
        tenant_id=state.request_row["tenant_id"],
        conversation_hash=state.request_row["conversation_hash"],
    )
    if (not material or material.get("sha256") != snapshot.get("content_sha256")
            or material.get("status") in {"failed", "deleted", "reupload_required"}):
        _log_binding_validation_failure(
            state, snapshot, None, "CANONICAL_MATERIAL_INVALID", historical=historical,
            stage="runtime_canonical_material_validation",
        )
        raise ProviderRequestError(code, "Canonical material is unavailable or changed")

    if snapshot.get("representation") == "gemini_inline_data":
        return {**snapshot, "provider_binding_id": None}

    row = await repo.get_provider_binding(
        material_id=snapshot["material_id"],
        connection_id=identity["connection_id"],
        account_scope_hash=identity["account_scope_id"],
        purpose=snapshot.get("purpose"),
        representation=snapshot.get("representation"),
    )
    if not row:
        _log_binding_validation_failure(
            state, snapshot, None, "BINDING_NOT_FOUND", historical=historical,
        )
        raise ProviderRequestError(code, "Frozen Provider binding is expired, replaced or unavailable")

    state_failure = provider_binding_state_failure_reason(row)
    if state_failure:
        _log_binding_validation_failure(
            state, snapshot, row, state_failure, historical=historical,
        )
        raise ProviderRequestError(code, "Frozen Provider binding is expired, replaced or unavailable")

    expected_generation = _generation(snapshot.get("binding_generation"))
    actual_generation = _generation(row.get("generation"), default=1)
    if expected_generation is None or actual_generation is None or actual_generation != expected_generation:
        _log_binding_validation_failure(
            state, snapshot, row, "BINDING_GENERATION_MISMATCH", historical=historical,
        )
        raise ProviderRequestError(code, "Frozen Provider binding is expired, replaced or unavailable")

    actual_uri = row.get("external_uri") or row.get("file_uri")
    if actual_uri != snapshot.get("external_uri"):
        _log_binding_validation_failure(
            state, snapshot, row, "BINDING_URI_MISMATCH", historical=historical,
        )
        raise ProviderRequestError(code, "Frozen Provider binding is expired, replaced or unavailable")

    expiry = row.get("expires_at")
    try:
        parsed = datetime.fromisoformat(str(expiry).replace("Z", "+00:00")) if expiry else None
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        margin = int(state.snapshot["material_placement_plan"]["binding_safety_seconds"])
        if parsed is None or parsed <= utcnow() + timedelta(seconds=margin):
            raise ValueError("expired or unknown expiry")
    except (TypeError, ValueError) as exc:
        _log_binding_validation_failure(
            state, snapshot, row, "BINDING_EXPIRED", historical=historical,
        )
        raise ProviderRequestError(
            code, "Historical file lifetime is insufficient; rebuild or migrate the Session"
        ) from exc

    return {**snapshot, "provider_binding_id": str(row["id"]), "expires_at": expiry}

async def verify_and_pin_historical(repo, state):
    placement = state.snapshot["material_placement_plan"]
    result = []
    seen = set()
    for occurrence in placement["verified_history_occurrences"]:
        binding = occurrence["binding"]
        key = (binding["material_id"], binding["binding_generation"], occurrence["part_digest"])
        if key in seen:
            continue
        seen.add(key)
        result.append(await checked_binding(repo, binding, state, historical=True))
    return result


async def bind_for_current_turn(binder, state, bind_legacy):
    # This callback consumes only the IDs already chosen by the execution plane.
    await bind_legacy(state)
    snapshots = [await checked_binding(binder.repo, b, state, historical=False) for b in state.material_bindings]
    # The installed binding must include the pin identity. bind_for_plan installs
    # the original snapshot; the enriched pin references are separate sealed facts.
    state.historical_pinned_bindings = await binder.verify_and_pin_historical(state)
    state.projected_material_occurrences = [{"binding": b} for b in snapshots]


def verify_pre_seal_material_invariants(state):
    placement = verify_placement(state.snapshot["material_placement_plan"], session=state.session, history=state.history)
    if [b["material_id"] for b in state.material_bindings] != placement["current_effective_material_ids"]:
        raise ProviderRequestError("CURRENT_TURN_MATERIAL_BINDING_MISMATCH", "Current bindings differ from the frozen placement")
    physical = state.provider_physical_plan or {}
    from app.protocol_adapters.wire_projection.gemini_wire import project_history_contents, project_current_user_content
    expected_history = project_history_contents(state.history)
    current = project_current_user_content(state.snapshot, material_ids=state.material_ids,
        material_bindings=state.material_bindings)
    full = physical.get("full_uncached_payload") or {}
    if full.get("contents") != expected_history + [current]:
        raise ProviderRequestError("WIRE_HISTORY_DIGEST_MISMATCH", "Native history or current material placement changed")
    verified = placement["verified_history_occurrences"]
    pins = {(b["material_id"], b["binding_generation"], b.get("external_uri")) for b in state.historical_pinned_bindings}
    if any((o["material_id"], o["binding_generation"], o["binding"].get("external_uri")) not in pins for o in verified):
        raise ProviderRequestError("HISTORICAL_MATERIAL_UNPINNED", "Historical occurrence has no verified resource reference")
    explicit = set(placement["current_explicit_material_ids"])
    occurrences = []
    for index, wrapped in enumerate(state.projected_material_occurrences):
        binding = wrapped["binding"]
        occurrences.append({**route_identity(state.session), "source_request_id": state.request_id,
            "material_id": binding["material_id"], "content_sha256": binding["content_sha256"],
            "binding_generation": binding["binding_generation"], "representation_kind": binding["representation"],
            "history_entry_index": placement["history_version"], "user_content_index": 2 * placement["history_version"],
            "part_index": index, "part_digest": stable_hash(current["parts"][index]),
            "origin": "request_explicit" if binding["material_id"] in explicit else "session_manifest_auto",
            "binding": {k: v for k, v in binding.items() if k != "inline_data"}})
    state.projected_material_occurrences = occurrences
    return occurrences
