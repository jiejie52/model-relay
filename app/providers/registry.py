from __future__ import annotations

from ..config import Settings
from .openai_compatible import OpenAICompatibleResponsesProvider


class ProviderRegistry:
    """Connection-aware provider registry.

    Legacy v1 callers still use ``get(provider)``. v2 resolves by connection_id
    after the server-side RouteResolver freezes a route. Since 0.5.1, connection
    availability defaults to ``all``; explicit ENABLED_CONNECTIONS filtering is
    applied only when CONNECTION_AVAILABILITY_MODE=allowlist.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.openai_compatible = OpenAICompatibleResponsesProvider(settings)
        self._v2: dict[str, object] = {}
        self._v2_meta: dict[str, dict[str, str]] = {}

    def _connection_is_enabled(self, connection_id: str) -> bool:
        checker = getattr(self.settings, "connection_is_enabled", None)
        if callable(checker):
            return bool(checker(connection_id))
        return connection_id in getattr(self.settings, "enabled_connection_set", set())

    def _connection_configuration(self, connection_id: str) -> tuple[bool, str | None]:
        checker = getattr(self.settings, "connection_configuration", None)
        if callable(checker):
            return checker(connection_id)
        return True, None

    def validate_enabled_connections(self) -> None:
        """Validate only explicitly restricted allowlist entries.

        In the default ``all`` mode a deployment may omit credentials for
        providers it does not use yet; those routes fail clearly when selected
        rather than preventing the whole API/Worker from starting.
        """
        if not bool(getattr(self.settings, "connection_restrictions_enabled", False)):
            return
        for connection_id in getattr(self.settings, "connection_allowlist_set", set()):
            configured, reason = self._connection_configuration(connection_id)
            if not configured:
                raise RuntimeError(
                    f"Connection {connection_id!r} is explicitly allowlisted but not configured: {reason}"
                )

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
        if not self._connection_is_enabled(connection_id):
            raise KeyError(
                f"Connection is disabled by Relay connection policy: {connection_id}"
            )
        try:
            return self._v2[connection_id]
        except KeyError as exc:
            configured, reason = self._connection_configuration(connection_id)
            if not configured:
                raise KeyError(
                    f"Connection is not configured on this deployment: {connection_id} ({reason})"
                ) from exc
            raise KeyError(f"Connection adapter is not registered: {connection_id}") from exc

    def get(self, provider: str):
        # v1 compatibility keeps the previous AIHubMix/OpenAI-compatible path.
        if self.settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required for legacy /v1/jobs execution")
        return self.openai_compatible
