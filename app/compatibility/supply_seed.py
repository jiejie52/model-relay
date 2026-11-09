"""Freeze same-account protocol candidates, then select once at first acceptance."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.control_plane.definitions import CapabilityContract, ModelOffering, ProtocolProfileSpec




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






