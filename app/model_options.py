from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from numbers import Real
from typing import Any


CAPABILITY_PROFILE_REVISION = "relay-model-options/2026-09-23.1"


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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.model = model
        self.option = option
        self.value = value

    def public_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "capability_revision": CAPABILITY_PROFILE_REVISION,
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
    warnings: tuple[str, ...] = ()


# Canonical options are provider-neutral application intent. Provider adapters
# are solely responsible for projecting these values to native wire fields.
_PROFILES: tuple[CapabilityProfile, ...] = (
    # Gemini 3.5/3.6 no longer expose legacy sampling controls in the same way;
    # keep output sizing canonical but fail closed on unsupported sampling knobs.
    CapabilityProfile(
        provider="gemini",
        model_pattern="gemini-3.6-*",
        supported_options=frozenset({"max_output_tokens"}),
        supported_think_levels=frozenset({"auto", "low", "medium", "high"}),
    ),
    CapabilityProfile(
        provider="gemini",
        model_pattern="gemini-3.5-*",
        supported_options=frozenset({"max_output_tokens"}),
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
    # The current Moonshot adapter has no explicit reasoning-effort wire mapping.
    # Fail closed rather than silently pretending low/medium/high took effect.
    CapabilityProfile(
        provider="kimi",
        model_pattern="kimi-*",
        supported_options=frozenset({"temperature", "top_p", "max_output_tokens"}),
        supported_think_levels=frozenset({"auto"}),
    ),
)

# v2.1 compatibility window: accept only legacy fields that have an unambiguous
# canonical meaning. Arbitrary provider wire passthrough is deliberately not
# carried into the v2.2 canonical-options contract.
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


def _number(value: Any, *, option: str, provider: str, model: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ModelOptionError(
            "OPTION_VALUE_INVALID",
            f"{option} must be a number",
            provider=provider,
            model=model,
            option=option,
            value=value,
        )
    return float(value)


def _validate_value(option: str, value: Any, *, provider: str, model: str) -> Any:
    if option == "temperature":
        numeric = _number(value, option=option, provider=provider, model=model)
        if numeric < 0 or numeric > 2:
            raise ModelOptionError(
                "OPTION_VALUE_INVALID",
                "temperature must be between 0 and 2",
                provider=provider,
                model=model,
                option=option,
                value=value,
            )
        return numeric
    if option == "top_p":
        numeric = _number(value, option=option, provider=provider, model=model)
        if numeric < 0 or numeric > 1:
            raise ModelOptionError(
                "OPTION_VALUE_INVALID",
                "top_p must be between 0 and 1",
                provider=provider,
                model=model,
                option=option,
                value=value,
            )
        return numeric
    if option == "max_output_tokens":
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ModelOptionError(
                "OPTION_VALUE_INVALID",
                "max_output_tokens must be a positive integer",
                provider=provider,
                model=model,
                option=option,
                value=value,
            )
        return int(value)
    raise ModelOptionError(
        "OPTION_UNSUPPORTED",
        f"Unsupported canonical model option: {option}",
        provider=provider,
        model=model,
        option=option,
        value=value,
    )


def resolve_model_options(
    *,
    provider: str,
    model: str,
    options: dict[str, Any] | None,
    provider_payload: dict[str, Any] | None = None,
    think_level: str | None = None,
) -> ResolvedModelOptions:
    """Resolve caller options into a frozen provider-neutral option set.

    ``provider_payload`` is accepted only as a v2.1 migration bridge. Known
    aliases are converted into canonical options; arbitrary provider wire fields
    fail closed instead of being merged into native provider JSON.
    """

    provider_n = str(provider or "").strip().lower()
    model_n = str(model or "").strip()
    profile = _profile(provider_n, model_n)
    requested = dict(options or {})
    warnings: list[str] = []
    requested_think_level = str(think_level or "auto").strip().lower() or "auto"
    if requested_think_level not in profile.supported_think_levels:
        raise ModelOptionError(
            "THINK_LEVEL_UNSUPPORTED",
            f"think_level={requested_think_level!r} is not supported by the selected provider/model",
            provider=provider_n,
            model=model_n,
            option="think_level",
            value=requested_think_level,
        )

    legacy = dict(provider_payload or {})
    if legacy:
        unsupported_legacy = sorted(
            key for key in legacy if key not in _LEGACY_ALIASES and key not in _LEGACY_IGNORED
        )
        if unsupported_legacy:
            raise ModelOptionError(
                "LEGACY_PROVIDER_PAYLOAD_UNSUPPORTED",
                "provider_payload is deprecated in Relay v2.2; unsupported legacy fields: "
                + ", ".join(unsupported_legacy),
                provider=provider_n,
                model=model_n,
                option=unsupported_legacy[0],
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
                    provider=provider_n,
                    model=model_n,
                    option=canonical_key,
                )
            requested.setdefault(canonical_key, legacy_value)
            warnings.append(
                f"provider_payload.{legacy_key} is deprecated; use options.{canonical_key}"
            )

    unsupported = sorted(set(requested) - set(profile.supported_options))
    if unsupported:
        raise ModelOptionError(
            "OPTION_UNSUPPORTED",
            "Model option is not supported by the selected provider/model: "
            + ", ".join(unsupported),
            provider=provider_n,
            model=model_n,
            option=unsupported[0],
        )

    effective: dict[str, Any] = {}
    for key in sorted(requested):
        effective[key] = _validate_value(
            key,
            requested[key],
            provider=provider_n,
            model=model_n,
        )

    return ResolvedModelOptions(
        requested_options={key: effective[key] for key in sorted(effective)},
        effective_options={key: effective[key] for key in sorted(effective)},
        requested_think_level=requested_think_level,
        effective_think_level=requested_think_level,
        capability_revision=CAPABILITY_PROFILE_REVISION,
        warnings=tuple(warnings),
    )


def effective_options(snapshot: dict[str, Any]) -> dict[str, Any]:
    value = snapshot.get("effective_options")
    if isinstance(value, dict):
        return dict(value)
    value = snapshot.get("options")
    if isinstance(value, dict):
        return dict(value)
    return {}
