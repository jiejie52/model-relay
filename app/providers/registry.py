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
        self._v2_meta: dict[str, dict[str, str]] = {}


    def validate_enabled_connections(self) -> None:
        enabled = self.settings.enabled_connection_set
        if "aihubmix_default" in enabled and self.settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required because aihubmix_default is enabled")
        if self.settings.aihubmix_gemini_connection_id in enabled:
            if self.settings.aihubmix_api_key is None:
                raise RuntimeError("AIHUBMIX_API_KEY is required because the Gemini native connection is enabled")
            if not self.settings.aihubmix_gemini_base_url:
                raise RuntimeError("AIHUBMIX_GEMINI_BASE_URL is required because the Gemini native connection is enabled")
        if self.settings.moonshot_connection_id in enabled and self.settings.moonshot_api_key is None:
            raise RuntimeError("MOONSHOT_API_KEY is required because the Moonshot connection is enabled")

    def register_v2(self, connection_id: str, adapter: object, *, provider: str | None = None) -> None:
        self._v2[connection_id] = adapter
        self._v2_meta[connection_id] = {
            "provider": str(provider or "").lower(),
            "adapter_version": str(getattr(adapter, "adapter_version", "unknown")),
        }

    def describe(self, connection_id: str) -> dict[str, str] | None:
        value = self._v2_meta.get(connection_id)
        if value is None:
            return None
        return {"connection_id": connection_id, **value}

    def registered_connections(self) -> list[str]:
        return sorted(self._v2)

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
