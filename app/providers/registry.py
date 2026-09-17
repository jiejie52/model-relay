from __future__ import annotations

from ..config import Settings
from .base import ProviderRequestError
from .moonshot import MoonshotChatCompletionsProvider
from .openai_compatible import OpenAICompatibleResponsesProvider


class ProviderRegistry:
    """Provider-neutral adapter registry.

    Unknown providers fail closed. Adding a provider should only require a new
    adapter/profile here; Job/Session storage and API contracts stay unchanged.
    """

    _OPENAI_COMPAT_ALIASES = {
        "grok",
        "xai",
        "gemini",
        "google",
        "openai-compatible",
        "openai_compatible",
        "aihubmix",
    }
    _MOONSHOT_ALIASES = {"moonshot", "kimi"}

    def __init__(self, settings: Settings) -> None:
        self.openai_compatible = OpenAICompatibleResponsesProvider(settings)
        self.moonshot = MoonshotChatCompletionsProvider(settings)

    @staticmethod
    def canonical_provider(provider: str) -> str:
        value = str(provider or "").strip().lower()
        if value in ProviderRegistry._MOONSHOT_ALIASES:
            return "moonshot"
        if value in ProviderRegistry._OPENAI_COMPAT_ALIASES:
            # Keep business-facing names such as grok/gemini for request routing
            # while using one transport adapter underneath.
            return value
        raise ProviderRequestError(
            "PROVIDER_UNSUPPORTED",
            f"Unsupported provider: {provider!r}",
        )

    def get(self, provider: str):
        value = str(provider or "").strip().lower()
        if value in self._MOONSHOT_ALIASES:
            return self.moonshot
        if value in self._OPENAI_COMPAT_ALIASES:
            return self.openai_compatible
        raise ProviderRequestError(
            "PROVIDER_UNSUPPORTED",
            f"Unsupported provider: {provider!r}",
        )

    def protocol_for(self, provider: str) -> str:
        return self.get(provider).protocol

    def history_codec_for(self, provider: str) -> str:
        return self.get(provider).history_codec
