"""Request-time supply-group qualification and one-time route pinning."""
from __future__ import annotations
from typing import Any
from app.core.idempotency import stable_hash

def context_settings(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return {"context_policy": value, "history_mode": "cumulative",
                "sticky_scope": "session", "workload_hint": "auto"}
    return {"context_policy": "conversation", **value.model_dump(mode="json")}


def _account_identity(resolver, connection_id: str) -> str:
    spec = resolver.config_snapshot.connection(connection_id) if resolver.config_snapshot else None
    if spec is None:
        return resolver._account_scope_hash(connection_id)
    return stable_hash([
        spec.channel_id, spec.account_id, spec.project_id, spec.region,
    ])


def resolve_supply_group(resolver, request, initial, policy, *, qualification_failures=None):
    from app.execution_plane.route_resolver import RouteResolutionError

    if not initial.supply_group_id:
        return initial, None, "pinned"
    candidates = []
    account = _account_identity(resolver, initial.connection_id)
    for entry in resolver.catalog.candidates(
        provider=initial.provider, model=initial.model, deployment_id=resolver.settings.deployment_id,
    ):
        if (entry.supply_group_id != initial.supply_group_id or entry.channel_id != initial.channel_id
                or entry.protocol not in {"gemini_native", "chat_completions"}
                or _account_identity(resolver, entry.connection_id) != account):
            continue
        try:
            candidate = resolver.resolve(
                provider=initial.provider, model=initial.model, purpose="session",
                requirements=request.capability_requirements.model_dump(mode="json"),
                offering_id=entry.offering_id,
                qualification_failures=qualification_failures,
            )
        except RouteResolutionError:
            if entry.unavailable_action == "reject_new_session":
                raise
            continue
        candidates.append(candidate)
    native = next((item for item in candidates if item.protocol == "gemini_native"), None)
    chat = next((item for item in candidates if item.protocol == "chat_completions"), None)
    if request.material_ids or policy["workload_hint"] == "multimodal_or_long_doc":
        if native is None:
            raise RouteResolutionError("ROUTE_CAPABILITY_UNAVAILABLE", "No qualified native supply in frozen group",
                                       provider=initial.provider, model=initial.model, purpose="session")
        return native, None, "pinned"
    if policy["workload_hint"] == "pure_text_qa":
        return chat or native or initial, None, "pinned"
    if native is None or chat is None:
        return native or chat or initial, None, "pinned"
    group = {
        "group_id": initial.supply_group_id,
        "candidates": [item.internal_metadata() for item in candidates],
        "context_settings": policy,
    }
    group["group_hash"] = stable_hash(group)
    return native, group, "pending_first_request_pin"


def materialize_supply_group(session: dict[str, Any], request: Any) -> dict[str, Any]:
    from app.execution_plane.semantic_resolver import ModelOptionError, resolve_model_options, validate_structured_output_capability
    from app.execution_plane.structured_output import StructuredOutputError
    from app.execution_plane.route_resolver import RouteResolutionError

    metadata = session.get("metadata") or {}
    if session.get("route_pin_state", "pinned") == "pinned":
        return metadata["_relay_route"]
    group = metadata.get("_relay_route_group") or {}
    candidates = group.get("candidates") or []
    native_required = bool(
        session.get("material_manifest") or request.material_ids
        or (request.requested_cache_mode == "on" and session.get("history_mode") != "ephemeral_turn")
    )
    prefer_chat = not native_required and (
        session.get("history_mode") == "ephemeral_turn" or bool(request.instructions)
    )
    preferred = "chat_completions" if prefer_chat else "gemini_native"
    ordered = [item for item in candidates if item.get("protocol") == preferred]
    if prefer_chat:
        ordered += [item for item in candidates if item.get("protocol") == "gemini_native"]
    for candidate in ordered:
        if (candidate.get("supply_group_id") != session.get("supply_group_id")
                or candidate.get("provider") != session["provider"] or candidate.get("model") != session["model"]):
            continue
        try:
            resolve_model_options(
                provider=session["provider"], model=session["model"], options=request.options,
                provider_payload=request.provider_payload, think_level=request.think_level,
                capability_contract=candidate.get("capability_contract"),
            )
            validate_structured_output_capability(
                provider=session["provider"], model=session["model"], structured_output=request.structured_output,
                capability_contract=candidate.get("capability_contract"),
            )
        except (ModelOptionError, StructuredOutputError):
            continue
        return candidate
    raise RouteResolutionError("ROUTE_CAPABILITY_UNAVAILABLE", "First request cannot materialize a qualified frozen supply",
                               provider=session["provider"], model=session["model"], purpose="request")

