from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ProviderResult:
    raw_bytes: bytes
    raw_json: dict[str, Any]
    text: str
    response_id: str | None
    usage: dict[str, Any]
    cached_tokens: int | None
    response_output: list[Any]
    # Opaque provider-native history delta. Relay Core stores and replays it but
    # does not reinterpret vendor reasoning/tool-call fields.
    history_record: dict[str, Any] | None = None
    finish_reason: str | None = None


class ProviderRequestError(RuntimeError):
    """Relay-side/provider-adapter validation error before an upstream request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProviderHTTPError(RuntimeError):
    """An upstream HTTP response with its original bytes and headers preserved."""

    def __init__(
        self,
        status_code: int,
        body: bytes,
        *,
        headers: list[tuple[str, str]] | None = None,
        content_type: str | None = None,
        provider: str | None = None,
        service: str | None = None,
        request_id: str | None = None,
        message: str = "Upstream provider request failed",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.headers = headers or []
        self.content_type = content_type
        self.provider = provider
        self.service = service or provider
        self.request_id = request_id


class ProviderTransportError(RuntimeError):
    """Network/transport failure where no upstream HTTP response was received."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        service: str | None = None,
        exception_type: str | None = None,
        cause_chain: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.service = service or provider
        self.exception_type = exception_type
        self.cause_chain = cause_chain or []


class ProviderAdapter(Protocol):
    provider_id: str
    protocol: str
    history_codec: str

    def capability_profile_version(self, model: str) -> str:
        ...

    async def execute(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any] | None,
        material_prefix: Any,
        history: list[dict[str, Any]],
    ) -> ProviderResult:
        ...
