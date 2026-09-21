from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class V2ProviderResult:
    raw_bytes: bytes
    raw_json: dict[str, Any]
    text: str
    response_id: str | None
    usage: dict[str, Any]
    cached_tokens: int | None
    response_output: Any
    history_entry: dict[str, Any]
    http_status: int | None = None
    provider_request_id: str | None = None


@dataclass
class V2ExecutionContext:
    snapshot: dict[str, Any]
    session: dict[str, Any]
    history: list[dict[str, Any]]
    material_ids: list[str]
    material_bindings: list[dict[str, Any]]
    tenant_id: str
    conversation_hash: str
    request_id: str | None = None
    session_id: str | None = None


class V2ProviderAdapter(Protocol):
    adapter_version: str

    async def execute(self, context: V2ExecutionContext) -> V2ProviderResult:
        ...
