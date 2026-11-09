"""Post-response business-contract validation, independent of wire projection."""
from app.control_plane.execution_authorization import execution_authorization
from app.protocol_adapters.base import ProviderRequestError


def validate_execution_observations(snapshot, observed):
    policy = execution_authorization(snapshot.get("execution_authorization"))
    fallback = observed.get("provider_fallback", observed.get("aihubmix_fallback"))
    paid = observed.get("paid_fallback")
    billing = observed.get("billing_tier")
    if policy["expected_billing_tier"] == "free" and billing == "paid": paid = True
    native = observed.get("native_constraint_enforced")
    guarantee = snapshot.get("structured_output_guarantee")
    if policy['evidence_source_mode']=='first_party_native_payload' and observed.get('evidence_source_mode')!='first_party_native_payload':
        raise ProviderRequestError('UPSTREAM_EVIDENCE_SOURCE_UNOBSERVED','First-party native evidence is missing')
    if policy['expected_billing_tier']=='official_direct' and billing not in (None,'official_direct'):
        raise ProviderRequestError('UPSTREAM_EVIDENCE_SOURCE_MISMATCH','Direct connection provenance did not match')
    if policy['expected_billing_tier'] in ('free','paid') and billing not in ('free','paid') and policy['on_unknown_billing']=='reject':
        raise ProviderRequestError('UPSTREAM_BILLING_UNOBSERVED','Connection provenance is not evidence of a free or paid billing tier')
    if fallback is True and not policy["allow_provider_fallback"]:
        raise ProviderRequestError("UPSTREAM_FALLBACK_NOT_AUTHORIZED", "The frozen supply did not authorize upstream fallback")
    if paid is True and not policy["allow_paid_fallback"]:
        raise ProviderRequestError("UPSTREAM_PAID_FALLBACK_NOT_AUTHORIZED", "Paid fallback has no frozen budget authorization")
    if (fallback is True and paid is None or policy["expected_billing_tier"] is not None and billing is None) and policy["on_unknown_billing"] == "reject":
        raise ProviderRequestError("UPSTREAM_BILLING_UNOBSERVED", "Fallback billing cannot be verified under the frozen policy")
    if observed.get("protocol_downgraded") is True:
        raise ProviderRequestError("UPSTREAM_PROTOCOL_CONTRACT_LOST", "Observed protocol downgrade violates the frozen execution contract")
    native_required = guarantee in {"native_constrained", "native_json_schema"}
    if native_required and (native is False or observed.get("json_repaired") is True):
        raise ProviderRequestError("UPSTREAM_NATIVE_CONSTRAINT_LOST", "Native generation constraint was explicitly lost")
    if native_required and native is None and policy["require_native_constraint_evidence"]:
        raise ProviderRequestError("UPSTREAM_NATIVE_CONSTRAINT_UNOBSERVED", "The frozen contract requires observable native-constraint evidence")
    return {"fallback": fallback if type(fallback) is bool else "unknown",
        "paid_fallback": paid if type(paid) is bool else "unknown", "billing_tier": billing or "unknown",
        "native_constraint": ("confirmed" if native is True else "rejected" if native is False else "unknown")
            if native_required else "not_required",
        "native_evidence_policy": "required" if policy["require_native_constraint_evidence"] else "certified_supply_with_observation"}
