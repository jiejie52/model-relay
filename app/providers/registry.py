from __future__ import annotations

from ..config import Settings
from .openai_compatible import OpenAICompatibleResponsesProvider


class ProviderRegistry:
    """Connection-aware provider registry.

    Legacy v1 callers still use ``get(provider)``.  v2 resolves by connection_id
    so provider family, wire protocol and deployment location are no longer the
    same concept.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.openai_compatible = OpenAICompatibleResponsesProvider(settings)
        self._v2: dict[str, object] = {}


    def validate_enabled_connections(self) -> None:
        enabled = self.settings.enabled_connection_set
        if "aihubmix_default" in enabled and self.settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required because aihubmix_default is enabled")
        if "moonshot_official" in enabled and self.settings.moonshot_api_key is None:
            raise RuntimeError("MOONSHOT_API_KEY is required because moonshot_official is enabled")

    def register_v2(self, connection_id: str, adapter: object) -> None:
        self._v2[connection_id] = adapter

    def get_v2(self, connection_id: str):
        if connection_id not in self.settings.enabled_connection_set:
            raise KeyError(f"Connection is not enabled in this deployment: {connection_id}")
        try:
            return self._v2[connection_id]
        except KeyError as exc:
            raise KeyError(f"Connection adapter is not registered: {connection_id}") from exc

    def get(self, provider: str):
        # v1 compatibility keeps the previous AIHubMix/OpenAI-compatible path.
        if self.settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required for legacy /v1/jobs execution")
        return self.openai_compatible
