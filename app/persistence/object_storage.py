from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol


@dataclass(frozen=True)
class ObjectLocation:
    storage_id: str
    bucket: str
    key: str


class ObjectStorage(Protocol):
    storage_id: str

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> ObjectLocation:
        ...

    async def put_stream(
        self, key: str, chunks: AsyncIterator[bytes], *, content_type: str
    ) -> ObjectLocation:
        ...

    async def get_bytes(self, location: ObjectLocation) -> bytes:
        ...

    async def get_stream(self, location: ObjectLocation) -> AsyncIterator[bytes]:
        ...

    async def head(self, location: ObjectLocation) -> dict[str, Any]:
        ...

    async def sign_read_url(self, location: ObjectLocation, *, expires_in: int) -> str:
        ...

    async def delete(self, location: ObjectLocation) -> None:
        ...


class StorageRegistry:
    def __init__(self, *backends: ObjectStorage) -> None:
        self._backends = {backend.storage_id: backend for backend in backends}

    def get(self, storage_id: str) -> ObjectStorage:
        try:
            return self._backends[storage_id]
        except KeyError as exc:
            raise KeyError(f"Object storage backend is not configured: {storage_id}") from exc
