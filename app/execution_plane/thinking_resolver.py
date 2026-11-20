"""Request intent resolution against a frozen contract; no wire construction."""
from app.thinking_contracts import (INTENTS, THINKING_CONTRACT_VERSION, EffectiveThinkingSemantics,
    ResolvedThinkingSemantics, ThinkingContractError, normalize_thinking_intent, thinking_active)


def ensure_v87_thinking_contract(contract, *, legacy_kimi_resolver=None):
    """Lift old frozen declarations in memory, preserving their certified mappings.

    Existing Requests never use this converter. An unsupported ordinary depth
    preference can use the old contract's declared AUTO execution; it does not
    acquire a new native depth or satisfy require. OFF never uses that fallback.
    """
    spec = contract.get("thinking") or {}
    if spec.get("version") == THINKING_CONTRACT_VERSION:
        return spec
    if spec.get("version"):
        raise ThinkingContractError("Unknown frozen thinking contract version")
    strategy = spec.get("wire_strategy") or "none"
    accepted = set(spec.get("accepted_levels") or ["auto"])
    level_map, budget_map = spec.get("level_map") or {}, spec.get("budget_map") or {}
    mappings = {}
    for intent in INTENTS:
        key = intent.lower()
        if intent == "OFF" and "none" in accepted: key = "none"
        target = level_map.get(key, key)
        allowed = key in accepted or key in level_map
        mode, action, level, budget = "adaptive", "omit", None, None
        satisfied = allowed and (key == target or intent == "AUTO")
        if strategy == "moonshot_legacy" and legacy_kimi_resolver:
            try:
                target = legacy_kimi_resolver(key)
                allowed = True
            except ValueError:
                allowed = False
            if target == "on":
                mode, action, level = "fixed_on", "send_toggle", "enabled"
                satisfied = intent == "AUTO"
            elif target != "auto": mode, action, level = "discrete", "send_level", target
        elif strategy == "gemini_thinking_budget":
            budget = budget_map.get(target)
            allowed = allowed and type(budget) is int
            mode, action = "budget", "send_budget"
            satisfied = allowed and (key == target or intent == "AUTO")
        elif strategy == "claude_adaptive_effort":
            if target == "off": mode, action, level = "toggle", "send_toggle", "disabled"
            elif target == "auto": mode, action = "adaptive", "send_adaptive"
            else: mode, action, level = "discrete", "send_level", target
        elif strategy == "chat_thinking_toggle":
            if target in {"on", "off"}: mode, action, level = "toggle", "send_toggle", "enabled" if target == "on" else "disabled"
        elif strategy == "chat_reasoning_effort_with_thinking_on" and target in {"on", "auto"}:
            mode, action, level = "fixed_on", "send_toggle", "enabled"
            satisfied = intent == "AUTO"
        elif target not in {"auto", "on", "off"} and strategy != "none":
            mode, action, level = "discrete", "send_level", target
        elif spec.get("mode") == "none": mode = "none"
        if intent == "OFF" and thinking_active({"mode": mode, "wire_action": action, "level": level, "budget_tokens": budget}):
            allowed = False
        projected_strategy = "kimi_thinking_toggle" if strategy == "moonshot_legacy" and mode == "fixed_on" else (
            "chat_reasoning_effort" if strategy == "moonshot_legacy" else strategy)
        mappings[intent] = {"rule_id": str(contract.get("contract_id", "legacy")) + ":legacy:" + intent,
            "is_allowed": allowed, "effective_mode": mode, "wire_action": action,
            "effective_level": level, "effective_budget": budget,
            "wire_strategy": projected_strategy,
            "satisfies_require": bool(allowed and satisfied), "mapping_reason": "legacy_frozen_mapping" if allowed else "legacy_intent_unsupported"}
    if mappings["AUTO"]["is_allowed"]:
        for intent in INTENTS[2:]:
            if not mappings[intent]["is_allowed"]:
                mappings[intent] = {**mappings["AUTO"],
                    "rule_id": str(contract.get("contract_id", "legacy")) + ":legacy:" + intent,
                    "satisfies_require": False, "mapping_reason": "legacy_prefer_declared_auto"}
    return {"version": THINKING_CONTRACT_VERSION, "wire_strategy": strategy, "mappings": mappings,
            "preserve_auto_mapping": True, "omit_on_explicit_auto": False,
            "when_thinking_active": spec.get("when_thinking_active") or {}}


def resolve_thinking_semantics(raw, field_present, policy, contract, *, legacy_kimi_resolver=None):
    intent, source = normalize_thinking_intent(raw, field_present)
    if policy not in {"prefer", "require"}:
        raise ThinkingContractError("Unknown thinking policy", field="think_level_policy")
    spec = ensure_v87_thinking_contract(contract, legacy_kimi_resolver=legacy_kimi_resolver)
    rule = spec["mappings"].get(intent)
    if not rule or not rule["is_allowed"]:
        exc = ThinkingContractError((rule or {}).get("mapping_reason", "intent_unmapped"), intent=intent)
        exc.code = "THINK_LEVEL_UNSUPPORTED"
        raise exc
    if policy == "require" and not rule["satisfies_require"]:
        exc = ThinkingContractError("Frozen mapping does not satisfy the hard thinking requirement", intent=intent)
        exc.code = "THINK_LEVEL_REQUIREMENT_UNSATISFIED"
        raise exc
    action, level, budget = rule["wire_action"], rule.get("effective_level"), rule.get("effective_budget")
    if intent == "AUTO" and rule["effective_mode"] != "fixed_on" and not spec.get("preserve_auto_mapping"):
        if source != "caller_explicit" or spec.get("omit_on_explicit_auto", True):
            action, level, budget = "omit", None, None
    effective = EffectiveThinkingSemantics(rule["effective_mode"], action,
        rule.get("wire_strategy") or spec.get("wire_strategy") or "none", level, budget)
    return ResolvedThinkingSemantics(raw, intent, source, policy, effective,
        rule["satisfies_require"], rule["rule_id"], rule["mapping_reason"]), spec


def validate_thinking_options(thinking, spec, options):
    e = thinking["effective"]
    rules = spec.get("when_thinking_active") or {}
    if thinking_active(e):
        for key in ("temperature", "top_p"):
            if key in options and (rules.get("forbid_" + key) is True or
                    key in rules and options[key] != rules[key]):
                exc = ThinkingContractError("options." + key + " conflicts with active thinking", field=key)
                exc.code = "OPTIONS_THINKING_CONFLICT"
                raise exc
    if e["wire_action"] == "send_budget" and e.get("budget_tokens", 0) > 0:
        maximum = options.get("max_output_tokens")
        if rules.get("budget_lt_max_output") and (maximum is None or e["budget_tokens"] >= maximum):
            exc = ThinkingContractError("Thinking budget must be smaller than the resolved output limit", field="max_output_tokens")
            exc.code = "OPTIONS_THINKING_CONFLICT"
            raise exc
