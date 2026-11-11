"""Deterministic projections of frozen effective thinking, never caller intent."""
from app.thinking_contracts import THINKING_CONTRACT_VERSION
from app.protocol_adapters.base import ProviderRequestError

# Registered strategies define implemented protocol/action intersections.
STRATEGIES = {
    "none": ({"gemini_native", "responses", "chat_completions", "claude_messages", "moonshot_chat"}, {"omit"}),
    "gemini_thinking_level": ({"gemini_native"}, {"omit", "send_level", "send_toggle"}),
    "gemini_thinking_budget": ({"gemini_native"}, {"omit", "send_budget", "send_toggle"}),
    "openai_reasoning_effort": ({"responses", "chat_completions"}, {"omit", "send_level"}),
    "responses_reasoning_effort": ({"responses"}, {"omit", "send_level"}),
    "chat_reasoning_effort": ({"chat_completions", "moonshot_chat"}, {"omit", "send_level"}),
    "chat_reasoning_effort_with_thinking_on": ({"chat_completions"}, {"omit", "send_level", "send_toggle"}),
    "chat_thinking_toggle": ({"chat_completions"}, {"omit", "send_toggle"}),
    "claude_thinking_budget": ({"claude_messages"}, {"omit", "send_budget", "send_toggle"}),
    "claude_adaptive_effort": ({"claude_messages"}, {"omit", "send_level", "send_toggle", "send_adaptive"}),
    "kimi_thinking_toggle": ({"moonshot_chat"}, {"omit", "send_toggle"}),
}


def validate_projection(spec, protocol):
    strategy = spec.get("wire_strategy") or "none"
    registration = STRATEGIES.get(strategy)
    if not registration or protocol not in registration[0]:
        raise ValueError("Thinking projection is not implemented for the supply protocol")
    for r in spec["mappings"].values():
        if not r["is_allowed"]: continue
        if r["wire_action"] not in registration[1]:
            raise ValueError("Thinking action is not implemented by the projection strategy")
        if strategy.startswith("gemini_") and r["wire_action"] == "send_toggle" and r.get("effective_level") != "disabled":
            raise ValueError("Gemini has no generic enable-toggle projection")
        if strategy.startswith("gemini_") and r["wire_action"] == "send_level" and r.get("effective_level") not in {"minimal", "low", "medium", "high"}:
            raise ValueError("Unregistered Gemini native thinking level")
        if r["wire_action"] == "send_budget" and r.get("effective_budget", 1) < 1:
            if strategy != "gemini_thinking_budget" or r["effective_budget"] not in {-1, 0}:
                raise ValueError("Special budget is not supported by this strategy")


def project_thinking(payload, snapshot, protocol):
    thinking = snapshot.get("thinking")
    if thinking is None:
        return False  # Persisted pre-8.7 requests retain their original projection.
    if thinking.get("version") != THINKING_CONTRACT_VERSION:
        raise ProviderRequestError("THINKING_SEMANTICS_INVALID", "Unknown frozen thinking version")
    e = thinking["effective"]
    strategy, action, level, budget = e["wire_strategy"], e["wire_action"], e.get("level"), e.get("budget_tokens")
    registered = STRATEGIES.get(strategy)
    if not registered or protocol not in registered[0] or action not in registered[1]:
        raise ProviderRequestError("THINKING_PROJECTION_UNSUPPORTED", "Frozen thinking has no registered projection")
    if action == "omit": return True
    if protocol == "gemini_native":
        native = {"thinkingLevel": level.upper()} if action == "send_level" else {"thinkingBudget": 0 if action == "send_toggle" else budget}
        payload.setdefault("generationConfig", {})["thinkingConfig"] = native
    elif protocol in {"responses", "chat_completions", "moonshot_chat"}:
        if action == "send_toggle": payload["thinking"] = {"type": level}
        else:
            if strategy == "chat_reasoning_effort_with_thinking_on": payload["thinking"] = {"type": "enabled"}
            if protocol == "responses": payload["reasoning"] = {"effort": level}
            else: payload["reasoning_effort"] = level
    elif protocol == "claude_messages":
        if action == "send_budget": payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif action == "send_toggle": payload["thinking"] = {"type": level}
        else:
            payload["thinking"] = {"type": "adaptive"}
            if action == "send_level": payload.setdefault("output_config", {})["effort"] = level
    return True
