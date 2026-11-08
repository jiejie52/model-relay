"""Versioned public-intent comparison against a Request's frozen capability."""
from app.core.idempotency import caller_intent_hash
from app.core.context_codec import normalize_structured_output


def semantic_intent_hash(intent, resolved):
    # Original UI labels remain in the snapshot for audit, not in 2.4 identity.
    # Mode on/off, material order, input strings and execution remain distinct.
    value = {**intent, "think_level": resolved.effective_think_level,
             "options": resolved.effective_options, "provider_payload": {},
             "structured_output": normalize_structured_output(intent.get("structured_output"))}
    return caller_intent_hash(value)
