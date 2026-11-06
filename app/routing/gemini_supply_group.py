"""Freeze same-account protocol candidates, then select once at first acceptance."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..control_plane import CapabilityContract, ModelOffering, ProtocolProfileSpec
from ..core.idempotency import stable_hash


def context_settings(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return {"context_policy": value, "history_mode": "cumulative",
                "sticky_scope": "session", "workload_hint": "auto"}
    return {"context_policy": "conversation", **value.model_dump(mode="json")}


def extend_gemini_supplies(contracts, profiles, offerings, settings):
    source_contracts = {item.contract_id: item for item in contracts}
    extended_contracts = list(contracts)
    extended_profiles = list(profiles)
    extended_offerings = list(offerings)
    chat_profile_id = "gemini-chat-aihubmix-v66"
    extended_profiles.append(ProtocolProfileSpec(
        profile_id=chat_profile_id, revision="relay-protocol-profile/gemini-chat/6.6.0",
        protocol="chat_completions",
        cache={
            "supported_mechanisms": ["implicit_prefix"], "usage_mapping": "openai_compatible",
            "affinity_header": "X-Aihubmix-Session-Id", "composite_mode": "implicit_only",
            "affinity_eviction_strategy": "ROUTER_STICKY_AND_HIT_RATE",
            "implicit_watchdog": True, "minimum_cacheable_tokens": 1024,
            "token_count_connection_id": settings.aihubmix_gemini_connection_id,
        },
    ))
    for index, offering in enumerate(offerings):
        if offering.provider != "gemini" or offering.connection_id != settings.aihubmix_gemini_connection_id:
            continue
        contract = source_contracts[offering.capability_contract_id]
        # Candidate model wildcards do not certify another protocol.
        if contract.cache.get("verification_status") not in {"verified", "published"}:
            continue
        group = f"gemini-aihubmix:{offering.model_pattern}"
        native_cache = {
            **contract.cache, "enable_payload_stripping": True,
            "affinity_eviction_strategy": "STATEFUL_HANDLE_STATUS_ONLY",
        }
        profiles_by_mechanism = {name: dict(cfg) for name, cfg in native_cache.get("mechanism_profiles", {}).items()}
        for name in ("stateful_resource", "implicit_prefix"):
            if name in profiles_by_mechanism:
                profiles_by_mechanism[name].setdefault("minimum_cacheable_tokens", 2048 if "pro" in offering.model_pattern else 1024)
        native_cache["mechanism_profiles"] = profiles_by_mechanism
        native_contract = replace(contract, contract_id=f"{contract.contract_id}-native-v66",
                                  revision=f"{contract.revision}/6.6", cache=native_cache)
        chat_contract = CapabilityContract(
            contract_id=f"{contract.contract_id}-chat-v66", revision="relay-capability/gemini-chat/6.6.0",
            supported_options=dict(contract.supported_options),
            thinking={**contract.thinking, "wire_strategy": "chat_reasoning_effort"},
            structured_output={"mode": "post_validate"},
            input_modalities=("text",), features=(),
            provider_defaults=dict(contract.provider_defaults),
            cache={
                "supported_mechanisms": ["implicit_prefix"], "verification_status": "verified",
                "composite_mode": "implicit_only", "implicit_watchdog": True,
                "affinity_eviction_strategy": "ROUTER_STICKY_AND_HIT_RATE",
                "mechanism_profiles": {"implicit_prefix": {
                    "verification_status": "verified", "threshold_mode": "provider_enforced",
                    "minimum_cacheable_tokens": 1024, "final_threshold_guard": True,
                }},
            },
        )
        extended_contracts.extend((native_contract, chat_contract))
        extended_offerings[index] = replace(offering, supply_group_id=group,
                                            capability_contract_id=native_contract.contract_id)
        extended_offerings.append(ModelOffering(
            offering_id=f"{offering.offering_id}-chat-v66", supply_group_id=group,
            provider="gemini", model_pattern=offering.model_pattern,
            connection_id=settings.aihubmix_chat_connection_id,
            capability_contract_id=chat_contract.contract_id, protocol_profile_id=chat_profile_id,
            cache_policy_id=offering.cache_policy_id, priority=offering.priority-1,
            deployment_id=offering.deployment_id, observed_model_policy=offering.observed_model_policy,
            quota=dict(offering.quota), metadata=dict(offering.metadata),
        ))
    return extended_contracts, extended_profiles, extended_offerings


def _account_identity(resolver, connection_id: str) -> str:
    spec = resolver.control_plane.connection(connection_id) if resolver.control_plane else None
    if spec is None:
        return resolver._account_scope_hash(connection_id)
    return stable_hash([
        spec.channel_id, spec.account_id, spec.metadata.get("region"),
        resolver.control_plane.credential(connection_id, resolver.settings),
    ])


def resolve_supply_group(resolver, request, initial, policy):
    from .route_resolver import RouteResolutionError

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
    from ..model_options import ModelOptionError, resolve_model_options, validate_structured_output_capability
    from ..structured_output import StructuredOutputError
    from .route_resolver import RouteResolutionError

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
