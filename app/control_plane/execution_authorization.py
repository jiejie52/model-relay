"""Supply-scoped authorization for observed routing, billing and generation guarantees."""
from app.immutable import freeze_json


def execution_authorization(value=None):
    raw = dict(value or {})
    allowed = {"allow_provider_fallback", "allow_paid_fallback", "paid_fallback_budget_ref",
               "allowed_actual_models", "require_native_constraint_evidence", "on_unknown_billing", "expected_billing_tier"}
    if set(raw) - allowed:
        raise ValueError("Unknown execution authorization field")
    result = {"allow_provider_fallback": False, "allow_paid_fallback": False,
        "paid_fallback_budget_ref": None, "allowed_actual_models": [],
        "require_native_constraint_evidence": False, "on_unknown_billing": "audit", "expected_billing_tier": None, **raw}
    for key in ("allow_provider_fallback", "allow_paid_fallback", "require_native_constraint_evidence"):
        if type(result[key]) is not bool: raise ValueError("Execution authorization flags must be boolean")
    if result["on_unknown_billing"] not in {"audit", "reject"}:
        raise ValueError("Invalid unknown billing policy")
    if result["expected_billing_tier"] not in {None, "free", "paid"}:
        raise ValueError("Invalid frozen billing tier")
    if not isinstance(result["allowed_actual_models"], (list, tuple)) or any(
            not isinstance(v, str) or not v.strip() for v in result["allowed_actual_models"]):
        raise ValueError("Allowed actual models must be explicit model names")
    if result["allow_paid_fallback"] and (not result["allow_provider_fallback"] or
            not isinstance(result["paid_fallback_budget_ref"], str) or not result["paid_fallback_budget_ref"].strip()):
        raise ValueError("Paid fallback requires explicit fallback and budget authorization")
    return freeze_json(result)


def validate_response_evidence_headers(value):
    import re
    if not isinstance(value, dict) or set(value) - {"paid_fallback", "billing_tier", "native_constraint_enforced", "protocol_downgraded"}:
        raise ValueError("Unknown response evidence mapping")
    for header in value.values():
        if not isinstance(header, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", header):
            raise ValueError("Invalid response evidence header")
