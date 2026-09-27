from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from numbers import Real
from typing import Any


CAPABILITY_PROFILE_REVISION = "relay-model-options/2026-09-27.1"


class ModelOptionError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: str,
        model: str,
        option: str | None = None,
        value: Any = None,
        capability_revision: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.model = model
        self.option = option
        self.value = value
        self.capability_revision = capability_revision or CAPABILITY_PROFILE_REVISION

    def public_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "capability_revision": self.capability_revision,
        }
        if self.option:
            detail["option"] = self.option
        return detail


@dataclass(frozen=True)
class CapabilityProfile:
    provider: str
    model_pattern: str
    supported_options: frozenset[str]
    supported_think_levels: frozenset[str]

    def matches(self, provider: str, model: str) -> bool:
        return self.provider == provider and fnmatchcase(model.lower(), self.model_pattern.lower())


@dataclass(frozen=True)
class ResolvedModelOptions:
    requested_options: dict[str, Any]
    effective_options: dict[str, Any]
    requested_think_level: str
    effective_think_level: str
    capability_revision: str
    capability_contract_id: str | None = None
    warnings: tuple[str, ...] = ()


# Compatibility profiles are used only for Sessions created from the legacy
# ROUTE_CATALOG_JSON contract, or for persisted Sessions that predate 2.0.
_PROFILES: tuple[CapabilityProfile, ...] = (
    CapabilityProfile(
        provider="gemini",
        model_pattern="gemini-3.6-*",
        supported_options=frozenset({"temperature", "max_output_tokens"}),
        supported_think_levels=frozenset({"auto", "low", "medium", "high"}),
    ),
    CapabilityProfile(
        provider="gemini",
        model_pattern="gemini-3.5-*",
        supported_options=frozenset({"temperature", "max_output_tokens"}),
        supported_think_levels=frozenset({"auto", "low", "medium", "high"}),
    ),
    CapabilityProfile(
        provider="gemini",
        model_pattern="gemini-*",
        supported_options=frozenset({"temperature", "top_p", "max_output_tokens"}),
        supported_think_levels=frozenset({"auto", "low", "medium", "high"}),
    ),
    CapabilityProfile(
        provider="grok",
        model_pattern="grok-4.6",
        supported_options=frozenset({"temperature", "top_p", "max_output_tokens"}),
        supported_think_levels=frozenset({"auto", "low", "medium", "high", "xhigh"}),
    ),
    CapabilityProfile(
        provider="grok",
        model_pattern="grok-*",
        supported_options=frozenset({"temperature", "top_p", "max_output_tokens"}),
        supported_think_levels=frozenset({"auto", "low", "medium", "high"}),
    ),
    CapabilityProfile(
        provider="kimi",
        model_pattern="kimi-k3*",
        supported_options=frozenset({"temperature", "top_p", "max_output_tokens"}),
        supported_think_levels=frozenset({"auto", "low", "high", "max"}),
    ),
    CapabilityProfile(
        provider="kimi",
        model_pattern="kimi-k2.7-code*",
        supported_options=frozenset({"temperature", "top_p", "max_output_tokens"}),
        supported_think_levels=frozenset({"auto"}),
    ),
    CapabilityProfile(
        provider="kimi",
        model_pattern="kimi-*",
        supported_options=frozenset({"temperature", "top_p", "max_output_tokens"}),
        supported_think_levels=frozenset({"auto"}),
    ),
)

_LEGACY_ALIASES: dict[str, str] = {
    "temperature": "temperature",
    "top_p": "top_p",
    "max_output_tokens": "max_output_tokens",
    "max_tokens": "max_output_tokens",
}
_LEGACY_IGNORED = frozenset({"material_mode"})


def _profile(provider: str, model: str) -> CapabilityProfile:
    provider_n = str(provider or "").strip().lower()
    model_n = str(model or "").strip()
    for profile in _PROFILES:
        if profile.matches(provider_n, model_n):
            return profile
    raise ModelOptionError(
        "OPTION_CAPABILITY_PROFILE_NOT_FOUND",
        "Relay has no model-option capability profile for the selected provider/model",
        provider=provider_n,
        model=model_n,
    )


def _number(value: Any, *, option: str, provider: str, model: str, revision: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ModelOptionError(
            "OPTION_VALUE_INVALID",
            f"{option} must be a number",
            provider=provider,
            model=model,
            option=option,
            value=value,
            capability_revision=revision,
        )
    return float(value)


def _validate_legacy_value(option: str, value: Any, *, provider: str, model: str) -> Any:
    return _validate_contract_value(
        option,
        value,
        rule={
            "type": "integer" if option == "max_output_tokens" else "number",
            "min": 1 if option == "max_output_tokens" else 0,
            "max": 2 if option == "temperature" else (1 if option == "top_p" else None),
        },
        provider=provider,
        model=model,
        revision=CAPABILITY_PROFILE_REVISION,
    )


def _validate_contract_value(
    option: str,
    value: Any,
    *,
    rule: dict[str, Any],
    provider: str,
    model: str,
    revision: str,
) -> Any:
    kind = str(rule.get("type") or ("integer" if option == "max_output_tokens" else "number"))
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ModelOptionError(
                "OPTION_VALUE_INVALID",
                f"{option} must be an integer",
                provider=provider,
                model=model,
                option=option,
                value=value,
                capability_revision=revision,
            )
        normalized: Any = int(value)
    elif kind == "number":
        normalized = _number(
            value,
            option=option,
            provider=provider,
            model=model,
            revision=revision,
        )
    elif kind == "string":
        if not isinstance(value, str):
            raise ModelOptionError(
                "OPTION_VALUE_INVALID",
                f"{option} must be a string",
                provider=provider,
                model=model,
                option=option,
                value=value,
                capability_revision=revision,
            )
        normalized = value
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ModelOptionError(
                "OPTION_VALUE_INVALID",
                f"{option} must be a boolean",
                provider=provider,
                model=model,
                option=option,
                value=value,
                capability_revision=revision,
            )
        normalized = value
    else:
        raise ModelOptionError(
            "OPTION_CONTRACT_INVALID",
            f"Capability contract has unsupported type {kind!r} for {option}",
            provider=provider,
            model=model,
            option=option,
            capability_revision=revision,
        )

    minimum = rule.get("min")
    maximum = rule.get("max")
    if minimum is not None and normalized < minimum:
        raise ModelOptionError(
            "OPTION_VALUE_INVALID",
            f"{option} must be >= {minimum}",
            provider=provider,
            model=model,
            option=option,
            value=value,
            capability_revision=revision,
        )
    if maximum is not None and normalized > maximum:
        raise ModelOptionError(
            "OPTION_VALUE_INVALID",
            f"{option} must be <= {maximum}",
            provider=provider,
            model=model,
            option=option,
            value=value,
            capability_revision=revision,
        )
    enum = rule.get("enum")
    if isinstance(enum, list) and normalized not in enum:
        raise ModelOptionError(
            "OPTION_VALUE_INVALID",
            f"{option} is not an allowed value",
            provider=provider,
            model=model,
            option=option,
            value=value,
            capability_revision=revision,
        )
    return normalized


def _merge_legacy_provider_payload(
    requested: dict[str, Any],
    provider_payload: dict[str, Any] | None,
    *,
    provider: str,
    model: str,
    revision: str,
) -> list[str]:
    warnings: list[str] = []
    legacy = dict(provider_payload or {})
    if not legacy:
        return warnings
    unsupported_legacy = sorted(
        key for key in legacy if key not in _LEGACY_ALIASES and key not in _LEGACY_IGNORED
    )
    if unsupported_legacy:
        raise ModelOptionError(
            "LEGACY_PROVIDER_PAYLOAD_UNSUPPORTED",
            "provider_payload is deprecated in Relay v2.2; unsupported legacy fields: "
            + ", ".join(unsupported_legacy),
            provider=provider,
            model=model,
            option=unsupported_legacy[0],
            capability_revision=revision,
        )
    for key in sorted(_LEGACY_IGNORED.intersection(legacy)):
        warnings.append(f"legacy provider_payload.{key} was ignored")
    for legacy_key, canonical_key in _LEGACY_ALIASES.items():
        if legacy_key not in legacy:
            continue
        legacy_value = legacy[legacy_key]
        if canonical_key in requested and requested[canonical_key] != legacy_value:
            raise ModelOptionError(
                "OPTION_CONFLICT",
                f"options.{canonical_key} conflicts with deprecated provider_payload.{legacy_key}",
                provider=provider,
                model=model,
                option=canonical_key,
                capability_revision=revision,
            )
        requested.setdefault(canonical_key, legacy_value)
        warnings.append(
            f"provider_payload.{legacy_key} is deprecated; use options.{canonical_key}"
        )
    return warnings


def _resolve_legacy(
    *,
    provider: str,
    model: str,
    options: dict[str, Any] | None,
    provider_payload: dict[str, Any] | None,
    think_level: str | None,
) -> ResolvedModelOptions:
    profile = _profile(provider, model)
    requested = dict(options or {})
    warnings = _merge_legacy_provider_payload(
        requested,
        provider_payload,
        provider=provider,
        model=model,
        revision=CAPABILITY_PROFILE_REVISION,
    )
    requested_think_level = str(think_level or "auto").strip().lower() or "auto"
    kimi_k27_always_thinking = provider == "kimi" and model.lower().startswith("kimi-k2.7-code")
    if not kimi_k27_always_thinking and requested_think_level not in profile.supported_think_levels:
        raise ModelOptionError(
            "THINK_LEVEL_UNSUPPORTED",
            f"think_level={requested_think_level!r} is not supported by the selected provider/model",
            provider=provider,
            model=model,
            option="think_level",
            value=requested_think_level,
        )
    unsupported = sorted(set(requested) - set(profile.supported_options))
    if unsupported:
        raise ModelOptionError(
            "OPTION_UNSUPPORTED",
            "Model option is not supported by the selected provider/model: " + ", ".join(unsupported),
            provider=provider,
            model=model,
            option=unsupported[0],
        )
    effective = {
        key: _validate_legacy_value(key, requested[key], provider=provider, model=model)
        for key in sorted(requested)
    }
    effective_think_level = requested_think_level
    if kimi_k27_always_thinking:
        effective_think_level = "on"
        warnings.append(
            f"kimi-k2.7-code is always-thinking; think_level={requested_think_level!r} was mapped to effective Thinking ON"
        )
    return ResolvedModelOptions(
        requested_options={key: effective[key] for key in sorted(effective)},
        effective_options={key: effective[key] for key in sorted(effective)},
        requested_think_level=requested_think_level,
        effective_think_level=effective_think_level,
        capability_revision=CAPABILITY_PROFILE_REVISION,
        warnings=tuple(warnings),
    )


def _kimi_legacy_thinking(
    *,
    model: str,
    requested: str,
    revision: str,
    provider: str,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    model_n = model.lower()
    if model_n.startswith("kimi-k2.7-code"):
        if requested not in {"auto", "low", "medium", "high", "xhigh", "max", "on", "off"}:
            raise ModelOptionError(
                "THINK_LEVEL_UNSUPPORTED",
                f"think_level={requested!r} is not a recognized Relay thinking intent",
                provider=provider,
                model=model,
                option="think_level",
                value=requested,
                capability_revision=revision,
            )
        if requested == "off":
            raise ModelOptionError(
                "THINK_LEVEL_UNSUPPORTED",
                "kimi-k2.7-code is always-thinking and cannot satisfy an explicit off requirement",
                provider=provider,
                model=model,
                option="think_level",
                value=requested,
                capability_revision=revision,
            )
        warnings.append(
            f"kimi-k2.7-code is always-thinking; think_level={requested!r} was mapped to effective Thinking ON"
        )
        return "on", warnings
    if model_n.startswith("kimi-k3"):
        if requested not in {"auto", "low", "high", "max"}:
            raise ModelOptionError(
                "THINK_LEVEL_UNSUPPORTED",
                f"think_level={requested!r} is not supported by the selected Kimi K3 capability",
                provider=provider,
                model=model,
                option="think_level",
                value=requested,
                capability_revision=revision,
            )
        return requested, warnings
    if requested != "auto":
        raise ModelOptionError(
            "THINK_LEVEL_UNSUPPORTED",
            f"think_level={requested!r} has no verified native mapping for this Kimi model",
            provider=provider,
            model=model,
            option="think_level",
            value=requested,
            capability_revision=revision,
        )
    return "auto", warnings


def resolve_model_options(
    *,
    provider: str,
    model: str,
    options: dict[str, Any] | None,
    provider_payload: dict[str, Any] | None = None,
    think_level: str | None = None,
    capability_contract: dict[str, Any] | None = None,
) -> ResolvedModelOptions:
    """Resolve caller intent against a frozen capability contract.

    New 2.0 Sessions freeze the full capability contract in RouteBinding. Later
    Requests resolve against that frozen snapshot, not against the latest global
    config release. Legacy Sessions continue through the old hard-coded profile
    compatibility path so existing recovery semantics remain valid.
    """

    provider_n = str(provider or "").strip().lower()
    model_n = str(model or "").strip()
    if not capability_contract:
        return _resolve_legacy(
            provider=provider_n,
            model=model_n,
            options=options,
            provider_payload=provider_payload,
            think_level=think_level,
        )

    contract = dict(capability_contract)
    revision = str(contract.get("revision") or CAPABILITY_PROFILE_REVISION)
    contract_id = str(contract.get("contract_id") or "") or None
    supported_options = contract.get("supported_options") or {}
    if isinstance(supported_options, list):
        supported_options = {str(name): {} for name in supported_options}
    if not isinstance(supported_options, dict):
        raise ModelOptionError(
            "OPTION_CONTRACT_INVALID",
            "Capability contract supported_options must be an object",
            provider=provider_n,
            model=model_n,
            capability_revision=revision,
        )

    requested = dict(options or {})
    warnings = _merge_legacy_provider_payload(
        requested,
        provider_payload,
        provider=provider_n,
        model=model_n,
        revision=revision,
    )
    unsupported = sorted(set(requested) - set(supported_options))
    if unsupported:
        raise ModelOptionError(
            "OPTION_UNSUPPORTED",
            "Model option is not supported by the frozen capability contract: " + ", ".join(unsupported),
            provider=provider_n,
            model=model_n,
            option=unsupported[0],
            capability_revision=revision,
        )

    requested_think_level = str(think_level or "auto").strip().lower() or "auto"
    thinking = contract.get("thinking") if isinstance(contract.get("thinking"), dict) else {}
    wire_strategy = str(thinking.get("wire_strategy") or "").lower()
    if wire_strategy == "moonshot_legacy":
        effective_think_level, extra = _kimi_legacy_thinking(
            model=model_n,
            requested=requested_think_level,
            revision=revision,
            provider=provider_n,
        )
        warnings.extend(extra)
    else:
        accepted = {
            str(x).strip().lower()
            for x in (thinking.get("accepted_levels") or ["auto"])
            if str(x).strip()
        }
        if requested_think_level not in accepted:
            raise ModelOptionError(
                "THINK_LEVEL_UNSUPPORTED",
                f"think_level={requested_think_level!r} is not supported by the frozen capability contract",
                provider=provider_n,
                model=model_n,
                option="think_level",
                value=requested_think_level,
                capability_revision=revision,
            )
        level_map = {
            str(k).strip().lower(): str(v).strip().lower()
            for k, v in dict(thinking.get("level_map") or {}).items()
        }
        effective_think_level = level_map.get(requested_think_level, requested_think_level)
        if effective_think_level != requested_think_level:
            warnings.append(
                f"think_level={requested_think_level!r} was normalized to effective {effective_think_level!r} by capability contract {contract_id or revision}"
            )

    requested_validated: dict[str, Any] = {}
    effective: dict[str, Any] = {}
    for key in sorted(requested):
        rule = supported_options.get(key)
        if not isinstance(rule, dict):
            rule = {}
        value = _validate_contract_value(
            key,
            requested[key],
            rule=rule,
            provider=provider_n,
            model=model_n,
            revision=revision,
        )
        requested_validated[key] = value
        effective[key] = value

    provider_defaults = contract.get("provider_defaults") or {}
    if not isinstance(provider_defaults, dict):
        raise ModelOptionError(
            "OPTION_CONTRACT_INVALID",
            "Capability contract provider_defaults must be an object",
            provider=provider_n,
            model=model_n,
            capability_revision=revision,
        )
    for key, raw_value in sorted(provider_defaults.items()):
        if key in effective:
            continue
        if key not in supported_options:
            raise ModelOptionError(
                "OPTION_CONTRACT_INVALID",
                f"Capability contract default references unsupported option {key!r}",
                provider=provider_n,
                model=model_n,
                option=key,
                capability_revision=revision,
            )
        rule = supported_options.get(key) if isinstance(supported_options.get(key), dict) else {}
        effective[key] = _validate_contract_value(
            key,
            raw_value,
            rule=rule,
            provider=provider_n,
            model=model_n,
            revision=revision,
        )
        warnings.append(f"capability contract applied provider default options.{key}")

    for key in sorted(effective):
        rule = supported_options.get(key) if isinstance(supported_options.get(key), dict) else {}
        allowed_levels = rule.get("allowed_when_think_levels")
        if isinstance(allowed_levels, list):
            allowed = {str(x).strip().lower() for x in allowed_levels}
            if effective_think_level not in allowed:
                raise ModelOptionError(
                    "OPTION_CONFLICT",
                    f"options.{key} is not valid when effective think_level={effective_think_level!r}",
                    provider=provider_n,
                    model=model_n,
                    option=key,
                    capability_revision=revision,
                )
        conflicts = rule.get("conflicts_with")
        if isinstance(conflicts, list):
            for other in conflicts:
                if str(other) in effective:
                    raise ModelOptionError(
                        "OPTION_CONFLICT",
                        f"options.{key} conflicts with options.{other}",
                        provider=provider_n,
                        model=model_n,
                        option=key,
                        capability_revision=revision,
                    )

    return ResolvedModelOptions(
        requested_options={key: requested_validated[key] for key in sorted(requested_validated)},
        effective_options={key: effective[key] for key in sorted(effective)},
        requested_think_level=requested_think_level,
        effective_think_level=effective_think_level,
        capability_revision=revision,
        capability_contract_id=contract_id,
        warnings=tuple(warnings),
    )


def validate_structured_output_capability(
    *,
    provider: str,
    model: str,
    structured_output: dict[str, Any] | None,
    capability_contract: dict[str, Any] | None,
) -> str:
    """Return the frozen guarantee mode, failing closed if it is unavailable."""
    requested = isinstance(structured_output, dict) and bool(structured_output)
    if not requested:
        return "none"
    if not capability_contract:
        return "legacy"
    revision = str(capability_contract.get("revision") or CAPABILITY_PROFILE_REVISION)
    structured = capability_contract.get("structured_output")
    mode = str((structured or {}).get("mode") if isinstance(structured, dict) else "none").lower()
    if mode in {"", "none"}:
        raise ModelOptionError(
            "STRUCTURED_OUTPUT_UNSUPPORTED",
            "Structured output is not supported by the frozen capability contract",
            provider=provider,
            model=model,
            option="structured_output",
            capability_revision=revision,
        )
    return mode


def effective_options(snapshot: dict[str, Any]) -> dict[str, Any]:
    value = snapshot.get("effective_options")
    if isinstance(value, dict):
        return dict(value)
    value = snapshot.get("options")
    if isinstance(value, dict):
        return dict(value)
    return {}
