from __future__ import annotations

from typing import Any

from ..supabase import SupabaseBackend
from ..utils import json_bytes


class ExecutionArchiveStore:
    """Existing Supabase Storage wrapper for request/response/history/error data."""

    def __init__(self, backend: SupabaseBackend) -> None:
        self.backend = backend

    async def put_bytes(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        upsert: bool = True,
    ) -> str:
        return await self.backend.storage_put(
            path,
            data,
            content_type=content_type,
            upsert=upsert,
        )

    async def put_json(self, path: str, value: Any, *, upsert: bool = True) -> str:
        return await self.backend.storage_put(
            path,
            json_bytes(value),
            content_type="application/json",
            upsert=upsert,
        )

    async def get_bytes(self, path: str) -> bytes:
        return await self.backend.storage_get(path)

    async def get_json(self, path: str) -> Any:
        return await self.backend.storage_get_json(path)
