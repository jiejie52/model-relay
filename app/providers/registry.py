from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Settings
from .gemini_native import GeminiNativeAdapter
from .grok_responses import GrokResponsesAdapter
from .moonshot_chat import MoonshotChatCompletionsAdapter
from .openai_compatible import OpenAICompatibleResponsesProvider


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    provider: str
    protocol: str
    api_origin: str
    account_scope: str
    credential_ref: str
    allowed_models: tuple[str, ...]
    capability_profile_version: str
    history_codec: str
    history_codec_version: str = "1"
    enabled: bool = True
    capabilities: dict[str, Any] = field(default_factory=dict)

    def allows_model(self, model: str) -> bool:
        return model in self.allowed_models or "*" in self.allowed_models

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("credential_ref", None)
        value["allowed_models"] = list(self.allowed_models)
        return value


class ProviderRegistry:
    """Legacy adapter lookup plus exact V2 profile resolution.

    `get(provider)` is kept only for legacy/Fusion compatibility. V2 must resolve
    a named server-side profile and never falls back to a universal adapter.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.openai_compatible = OpenAICompatibleResponsesProvider(settings)
        self._profiles: dict[str, ProviderProfile] = {}
        self._adapters: dict[str, Any] = {}
        self._register_builtins()
        self._register_overrides()

    def get(self, provider: str):
        # Legacy behavior only. New code must call get_v2(profile_name).
        return self.openai_compatible

    def get_profile(self, profile_name: str) -> ProviderProfile:
        profile = self._profiles.get(profile_name)
        if profile is None:
            raise KeyError(f"Unknown upstream_profile: {profile_name}")
        if not profile.enabled:
            raise KeyError(f"Upstream profile is disabled: {profile_name}")
        return profile

    def get_v2(self, profile_name: str):
        profile = self.get_profile(profile_name)
        adapter = self._adapters.get(profile_name)
        if adapter is None:
            raise KeyError(f"No adapter registered for upstream_profile: {profile_name}")
        return adapter

    def validate_session_model(self, profile_name: str, provider: str, model: str) -> ProviderProfile:
        profile = self.get_profile(profile_name)
        if profile.provider != provider:
            raise ValueError(
                f"upstream_profile {profile_name} belongs to provider={profile.provider}, not {provider}"
            )
        if not profile.allows_model(model):
            raise ValueError(f"model {model} is not allowed by upstream_profile {profile_name}")
        return profile

    def capabilities(self) -> list[dict[str, Any]]:
        return [self._profiles[name].public_dict() for name in sorted(self._profiles)]

    def _register(self, profile: ProviderProfile) -> None:
        self._profiles[profile.name] = profile
        if not profile.enabled:
            return
        if profile.provider == "moonshot" and profile.protocol == "chat_completions":
            self._adapters[profile.name] = MoonshotChatCompletionsAdapter(self.settings, profile)
        elif profile.provider == "grok" and profile.protocol == "responses":
            self._adapters[profile.name] = GrokResponsesAdapter(self.settings, profile)
        elif profile.provider == "gemini" and profile.protocol == "generate_content":
            self._adapters[profile.name] = GeminiNativeAdapter(self.settings, profile)

    def _register_builtins(self) -> None:
        self._register(
            ProviderProfile(
                name="moonshot-official-chat",
                provider="moonshot",
                protocol="chat_completions",
                api_origin=self.settings.moonshot_root,
                account_scope="moonshot-official-default",
                credential_ref="MOONSHOT_API_KEY",
                allowed_models=("kimi-k3", "kimi-k2.6"),
                capability_profile_version="moonshot-chat-2026-09-17",
                history_codec="moonshot-chat-native",
                enabled=self.settings.moonshot_api_key is not None,
                capabilities={
                    "structured_output": ["json_object", "json_schema"],
                    "material_modes": ["image-base64", "file-extract", "text"],
                    "tools": "deliver_only",
                    "allow_model_switch": False,
                },
            )
        )
        self._register(
            ProviderProfile(
                name="grok-aihubmix-responses",
                provider="grok",
                protocol="responses",
                api_origin=self.settings.aihubmix_root,
                account_scope="aihubmix-default",
                credential_ref="AIHUBMIX_API_KEY",
                allowed_models=("grok-4.5", "grok-4.6"),
                capability_profile_version="grok-responses-2026-09-17",
                history_codec="grok-responses-native",
                enabled=self.settings.aihubmix_api_key is not None,
                capabilities={
                    "structured_output": ["json_object", "json_schema"],
                    "material_modes": ["presigned-url"],
                    "reasoning_effort": ["low", "medium", "high", "xhigh"],
                    "allow_model_switch": True,
                    "history_compatibility_group": "grok-4x-responses",
                },
            )
        )
        self._register(
            ProviderProfile(
                name="gemini-native",
                provider="gemini",
                protocol="generate_content",
                api_origin=self.settings.gemini_root,
                account_scope="gemini-default",
                credential_ref="GEMINI_API_KEY",
                allowed_models=(
                    "gemini-3.1-flash-lite",
                    "gemini-3.1-flash",
                    "gemini-3.1-pro",
                    "gemini-2.5-flash",
                    "gemini-2.5-pro",
                ),
                capability_profile_version="gemini-native-2026-09-17",
                history_codec="gemini-native",
                enabled=self.settings.gemini_api_key is not None,
                capabilities={
                    "structured_output": ["json_object", "json_schema"],
                    "material_modes": ["inlineData", "Files API/fileData"],
                    "allow_model_switch": False,
                },
            )
        )

    def _register_overrides(self) -> None:
        for name, raw in self.settings.provider_profile_overrides().items():
            if not isinstance(raw, dict):
                raise ValueError(f"Profile override {name} must be an object")
            provider = str(raw.get("provider") or "").strip().lower()
            protocol = str(raw.get("protocol") or "").strip()
            if provider not in {"moonshot", "grok", "gemini"}:
                raise ValueError(f"Unsupported V2 provider in profile {name}: {provider}")
            defaults = self._profiles.get(name)
            profile = ProviderProfile(
                name=name,
                provider=provider,
                protocol=protocol,
                api_origin=str(raw.get("api_origin") or (defaults.api_origin if defaults else "")).rstrip("/"),
                account_scope=str(raw.get("account_scope") or (defaults.account_scope if defaults else name)),
                credential_ref=str(raw.get("credential_ref") or (defaults.credential_ref if defaults else "")),
                allowed_models=tuple(str(x) for x in (raw.get("allowed_models") or (defaults.allowed_models if defaults else []))),
                capability_profile_version=str(raw.get("capability_profile_version") or (defaults.capability_profile_version if defaults else "custom-1")),
                history_codec=str(raw.get("history_codec") or (defaults.history_codec if defaults else f"{provider}-native")),
                history_codec_version=str(raw.get("history_codec_version") or (defaults.history_codec_version if defaults else "1")),
                enabled=bool(raw.get("enabled", True)),
                capabilities=dict(raw.get("capabilities") or (defaults.capabilities if defaults else {})),
            )
            # The credential must still exist in the typed Settings; a profile JSON
            # cannot inject a secret value.
            if profile.credential_ref == "MOONSHOT_API_KEY" and self.settings.moonshot_api_key is None:
                profile = ProviderProfile(**{**asdict(profile), "enabled": False})
            elif profile.credential_ref == "AIHUBMIX_API_KEY" and self.settings.aihubmix_api_key is None:
                profile = ProviderProfile(**{**asdict(profile), "enabled": False})
            elif profile.credential_ref == "GEMINI_API_KEY" and self.settings.gemini_api_key is None:
                profile = ProviderProfile(**{**asdict(profile), "enabled": False})
            self._register(profile)
