"""Shared thinking vocabulary and immutable data contracts, without execution I/O."""
from dataclasses import asdict, dataclass
from typing import Any, Literal

THINKING_CONTRACT_VERSION = "relay-thinking/2"
INTENTS = ("AUTO", "OFF", "LOW", "MEDIUM", "HIGH", "XHIGH", "MAX")
MODES = frozenset({"none", "toggle", "fixed_on", "discrete", "budget", "adaptive"})
ACTIONS = frozenset({"omit", "send_level", "send_budget", "send_toggle", "send_adaptive"})
OFF_ALIASES = frozenset({"NONE", "DISABLED", "FALSE"})


class ThinkingContractError(ValueError):
    def __init__(self, message, *, field="thinking", intent=None):
        super().__init__(message)
        self.field, self.intent = field, intent


@dataclass(frozen=True)
class EffectiveThinkingSemantics:
    mode: str
    wire_action: str
    wire_strategy: str
    level: str | None = None
    budget_tokens: int | None = None


@dataclass(frozen=True)
class ResolvedThinkingSemantics:
    requested_raw: str | None
    requested_intent: str
    source: Literal["omitted", "explicit_empty", "caller_explicit"]
    policy: Literal["prefer", "require"]
    effective: EffectiveThinkingSemantics
    satisfies_require: bool
    mapping_rule_id: str
    mapping_reason: str
    version: str = THINKING_CONTRACT_VERSION

    def canonical(self):
        return asdict(self)


@dataclass(frozen=True)
class ObservedThinkingEvidence:
    # None is unknown, not evidence of zero consumption or disabled thinking.
    reasoning_tokens: int | None = None
    has_explicit_thought_blocks: bool | None = None
    thinking_parameter_stripped: bool | None = None
    thinking_degraded: bool | None = None
    effective_mode: str | None = None
    evidence_source_mode: str | None = None


def normalize_thinking_intent(raw, field_present):
    if not field_present:
        return "AUTO", "omitted"
    if raw is None or isinstance(raw, str) and not raw.strip():
        return "AUTO", "explicit_empty"
    if not isinstance(raw, str):
        raise ThinkingContractError("think_level must be a string", field="think_level")
    token = raw.strip().upper()
    if token in OFF_ALIASES:
        token = "OFF"
    if token not in INTENTS:
        raise ThinkingContractError("Unknown thinking intent", field="think_level")
    return token, "caller_explicit"


def thinking_active(effective):
    if effective.get("mode") == "none" or effective.get("level") in {"disabled", "none", "off"}:
        return False
    if effective.get("wire_action") == "send_budget" and effective.get("budget_tokens") == 0:
        return False
    return True


def thinking_identity(thinking):
    """Only execution and acceptance boundaries participate in Request identity."""
    if not isinstance(thinking, dict) or thinking.get("version") != THINKING_CONTRACT_VERSION:
        raise ThinkingContractError("Unsupported frozen thinking semantics")
    return {"version": THINKING_CONTRACT_VERSION, "effective": thinking["effective"],
            "policy": thinking["policy"]}


def derive_thinking_summary(spec):
    rules = spec["mappings"]
    allowed = [i for i in INTENTS if rules[i]["is_allowed"]]
    modes = sorted({rules[i]["effective_mode"] for i in allowed if i not in {"AUTO", "OFF"}})
    return {"primary_mode": modes[0] if len(modes) == 1 else "mixed" if modes else "none",
            "allowed_intents": allowed,
            "hard_satisfied_intents": [i for i in allowed if rules[i]["satisfies_require"]],
            "supports_thinking": any(thinking_active({"mode": rules[i]["effective_mode"],
                "wire_action": rules[i]["wire_action"], "level": rules[i].get("effective_level"),
                "budget_tokens": rules[i].get("effective_budget")}) for i in allowed)}


def validate_thinking_contract(spec):
    """Structural checks shared by declarative and Excel configuration routes."""
    def fail(message, field="thinking", intent=None):
        raise ThinkingContractError(message, field=field, intent=intent)
    if spec.get("version") != THINKING_CONTRACT_VERSION:
        fail("Unknown thinking contract version", "version")
    if "accepted_levels" in spec:
        fail("Version 2 thinking uses mappings only", "accepted_levels")
    if "preserve_auto_mapping" in spec:
        fail("Legacy AUTO preservation is not a new-contract configuration field", "preserve_auto_mapping")
    minimum, maximum = spec.get("minimum_budget", 1), spec.get("maximum_budget", 2**31 - 1)
    if type(minimum) is not int or type(maximum) is not int or minimum > maximum:
        fail("Budget bounds must be ordered integers", "minimum_budget/maximum_budget")
    rules = spec.get("mappings")
    if not isinstance(rules, dict) or set(rules) != set(INTENTS):
        fail("Exactly seven intent mappings are required", "mappings")
    if type(spec.get("omit_on_explicit_auto", True)) is not bool:
        fail("Explicit AUTO omission must be boolean", "omit_on_explicit_auto")
    active = spec.get("when_thinking_active") or {}
    if not isinstance(active, dict) or set(active) - {"forbid_temperature", "forbid_top_p", "temperature", "top_p", "budget_lt_max_output"}:
        fail("Unknown active-thinking option constraint", "when_thinking_active")
    for key in ("forbid_temperature", "forbid_top_p", "budget_lt_max_output"):
        if key in active and type(active[key]) is not bool:
            fail("Thinking option flags must be boolean", "when_thinking_active." + key)
    for key in ("temperature", "top_p"):
        if key in active:
            import math
            if type(active[key]) not in (int, float) or not math.isfinite(active[key]):
                fail("Fixed sampling controls must be finite numbers", "when_thinking_active." + key)
            if active.get("forbid_" + key):
                fail("A forbidden sampling control cannot also be fixed", "when_thinking_active." + key)
    budgets, levels = [], []
    rank = {v: n for n, v in enumerate(("none", "minimal", "low", "medium", "high", "xhigh", "max"))}
    fixed = any(isinstance(r, dict) and r.get("is_allowed") and r.get("effective_mode") == "fixed_on" for r in rules.values())
    for intent in INTENTS:
        r = rules[intent]
        if not isinstance(r, dict): fail("Mapping must be an object", "mappings", intent)
        if "wire_strategy" in r: fail("Projection strategy belongs to the contract", "wire_strategy", intent)
        if type(r.get("is_allowed")) is not bool or type(r.get("satisfies_require")) is not bool:
            fail("Mapping flags must be booleans", "is_allowed/satisfies_require", intent)
        if not isinstance(r.get("mapping_reason"), str) or not r["mapping_reason"].strip():
            fail("Mapping reason is required", "mapping_reason", intent)
        if not r.get("rule_id"): fail("Rule identity is required", "rule_id", intent)
        if not r["is_allowed"]:
            if r["satisfies_require"]: fail("Denied rules cannot satisfy require", "satisfies_require", intent)
            continue
        mode, action = r.get("effective_mode"), r.get("wire_action")
        level, budget = r.get("effective_level"), r.get("effective_budget")
        if mode not in MODES or action not in ACTIONS:
            fail("Unknown mode or wire action", "effective_mode/wire_action", intent)
        if action == "send_level" and (mode != "discrete" or not isinstance(level, str) or not level):
            fail("Level projection requires discrete mode and a level", "effective_level", intent)
        if action == "send_toggle" and (mode not in {"toggle", "fixed_on"} or level not in {"enabled", "disabled"}):
            fail("Toggle projection requires an enabled/disabled state", "effective_level", intent)
        if action == "send_adaptive" and mode != "adaptive":
            fail("Adaptive projection requires adaptive mode", "effective_mode", intent)
        if action == "send_budget":
            minimum = spec.get("minimum_budget", 1)
            maximum = spec.get("maximum_budget", 2**31 - 1)
            if mode != "budget" or type(budget) is not int or not minimum <= budget <= maximum:
                fail("Budget is outside the declared integer bounds", "effective_budget", intent)
        if action not in {"send_level", "send_toggle"} and level is not None:
            fail("This action cannot carry a level", "effective_level", intent)
        if action != "send_budget" and budget is not None:
            fail("This action cannot carry a budget", "effective_budget", intent)
        if mode == "none" and action != "omit": fail("None mode must omit", "wire_action", intent)
        if mode == "fixed_on" and (action != "send_toggle" or level != "enabled"):
            fail("Fixed-on execution must explicitly enable thinking", "wire_action", intent)
        if fixed and intent == "OFF": fail("Fixed-on contracts must reject OFF", "is_allowed", intent)
        if intent == "OFF" and thinking_active({"mode": mode, "wire_action": action, "level": level, "budget_tokens": budget}):
            fail("OFF cannot map to active thinking, even under prefer", "wire_action", intent)
        if mode == "fixed_on" and intent not in {"AUTO", "OFF"} and r["satisfies_require"]:
            fail("Fixed-on does not certify a requested depth", "satisfies_require", intent)
        if intent in INTENTS[2:]:
            if action == "send_budget" and budget >= 0:
                if budgets and budget < budgets[-1]: fail("Budget mapping is not monotone", "effective_budget", intent)
                budgets.append(budget)
            if action == "send_level":
                if level not in rank: fail("Discrete level has no declared ordering", "effective_level", intent)
                if levels and rank[level] < levels[-1]: fail("Level mapping is not monotone", "effective_level", intent)
                levels.append(rank[level])
    if not rules["AUTO"]["is_allowed"]: fail("AUTO must be callable", "is_allowed", "AUTO")
    if budgets != sorted(budgets) or levels != sorted(levels):
        fail("Thinking mappings must be monotone", "effective_budget/effective_level")
    return derive_thinking_summary(spec)
