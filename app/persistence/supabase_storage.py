from __future__ import annotations

from typing import Any, AsyncIterator

from .object_storage import ObjectLocation
from ..config import Settings
from ..supabase import SupabaseBackend


class SupabaseObjectStorage:
    storage_id = "supabase_shared"

    def __init__(self, backend: SupabaseBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings
        self.storage_id = settings.default_storage_id

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> ObjectLocation:
        await self.backend.storage_put(key, data, content_type=content_type)
        return ObjectLocation(self.storage_id, self.settings.supabase_bucket, key)

    async def put_stream(
        self, key: str, chunks: AsyncIterator[bytes], *, content_type: str
    ) -> ObjectLocation:
        # Supabase REST upload is currently buffered by this backend.  The
        # interface is streaming-shaped so R2/OSS implementations can use native
        # multipart streaming without changing Relay core call sites.
        data = bytearray()
        async for chunk in chunks:
            data.extend(chunk)
        return await self.put_bytes(key, bytes(data), content_type=content_type)

    async def get_bytes(self, location: ObjectLocation) -> bytes:
        self._check(location)
        return await self.backend.storage_get(location.key)

    async def get_stream(self, location: ObjectLocation) -> AsyncIterator[bytes]:
        yield await self.get_bytes(location)

    async def head(self, location: ObjectLocation) -> dict[str, Any]:
        self._check(location)
        return await self.backend.storage_head(location.key)

    async def sign_read_url(self, location: ObjectLocation, *, expires_in: int) -> str:
        self._check(location)
        return await self.backend.storage_sign_read_url(location.key, expires_in=expires_in)

    async def delete(self, location: ObjectLocation) -> None:
        self._check(location)
        await self.backend.storage_delete(location.key)

    def _check(self, location: ObjectLocation) -> None:
        if location.storage_id != self.storage_id:
            raise ValueError(
                f"Storage mismatch: object={location.storage_id}, backend={self.storage_id}"
            )
