from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class MaterialFile:
    material_id: str
    tenant_id: str
    conversation_hash: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    data: bytes


@dataclass
class ProviderFileResult:
    binding: dict[str, Any]
    raw_response: bytes | None = None
    raw_response_content_type: str | None = None
    request_id: str | None = None
    phase: str = "ready"
    derived_object_id: str | None = None
    http_status: int | None = None


class ProviderFileAdapter(Protocol):
    provider: str
    adapter_version: str
    connection_id: str
    account_scope_hash: str

    async def prepare(self, material: MaterialFile, *, generation: int) -> ProviderFileResult:
        ...

    async def probe(self, binding: dict[str, Any]) -> dict[str, Any]:
        ...

    async def delete(self, binding: dict[str, Any]) -> None:
        ...
