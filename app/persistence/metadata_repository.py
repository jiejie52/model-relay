from __future__ import annotations

from typing import Any, Protocol


class MetadataRepository(Protocol):
    """The v2 core depends on this contract, not on Supabase-specific REST calls."""

    async def get_request(self, request_id: str, **owner: Any) -> dict[str, Any] | None:
        ...

    async def get_material(self, material_id: str, **owner: Any) -> dict[str, Any] | None:
        ...

    async def get_object(self, object_id: str, **owner: Any) -> dict[str, Any] | None:
        ...
