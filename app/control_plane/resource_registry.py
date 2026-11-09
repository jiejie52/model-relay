from __future__ import annotations


class CacheResourceRegistry:
    def __init__(self) -> None:
        self._versions = {}
        self._items: dict[str, object] = {}

    def register(self, connection_id: str, adapter: object) -> None:
        identity = getattr(getattr(adapter, "connection", None), "identity_hash", None)
        if identity: self._versions[(connection_id, identity)] = adapter
        self._items[connection_id] = adapter

    def maybe_get(self, connection_id: str):
        from app.runtime.connection_scope import current_identity
        identity = current_identity()
        return self._versions.get((connection_id, identity)) if identity else self._items.get(connection_id)

    def get(self, connection_id: str):
        from app.runtime.connection_scope import current_identity
        identity = current_identity()
        if identity:
            try: return self._versions[(connection_id, identity)]
            except KeyError: raise KeyError("Frozen resource connection version is not registered") from None
        try:
            return self._items[connection_id]
        except KeyError as exc:
            raise KeyError(f"Cache resource adapter is not registered: {connection_id}") from exc

    def registered_connections(self) -> list[str]:
        return sorted(self._items)
