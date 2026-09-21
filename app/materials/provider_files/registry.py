from __future__ import annotations

from typing import Any

from .base import ProviderFileAdapter


class ProviderFileRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProviderFileAdapter] = {}

    def register(self, connection_id: str, adapter: ProviderFileAdapter) -> None:
        self._adapters[connection_id] = adapter

    def get(self, connection_id: str) -> ProviderFileAdapter:
        try:
            return self._adapters[connection_id]
        except KeyError as exc:
            raise KeyError(f"Provider file adapter is not registered: {connection_id}") from exc

    def maybe_get(self, connection_id: str | None) -> ProviderFileAdapter | None:
        if not connection_id:
            return None
        return self._adapters.get(connection_id)

    def has(self, connection_id: str | None) -> bool:
        return bool(connection_id and connection_id in self._adapters)

    def registered_connections(self) -> list[str]:
        return sorted(self._adapters)

    def describe(self, connection_id: str) -> dict[str, Any] | None:
        adapter = self._adapters.get(connection_id)
        if adapter is None:
            return None
        return {
            "connection_id": connection_id,
            "provider": adapter.provider,
            "adapter_version": adapter.adapter_version,
            "account_scope_hash": adapter.account_scope_hash,
        }
