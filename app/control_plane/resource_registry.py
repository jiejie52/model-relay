from __future__ import annotations


class CacheResourceRegistry:
    def __init__(self) -> None:
        self._items: dict[str, object] = {}

    def register(self, connection_id: str, adapter: object) -> None:
        self._items[connection_id] = adapter

    def maybe_get(self, connection_id: str):
        return self._items.get(connection_id)

    def get(self, connection_id: str):
        try:
            return self._items[connection_id]
        except KeyError as exc:
            raise KeyError(f"Cache resource adapter is not registered: {connection_id}") from exc

    def registered_connections(self) -> list[str]:
        return sorted(self._items)
