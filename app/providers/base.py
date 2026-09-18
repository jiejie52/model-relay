from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass
class ProviderResult:
    raw_bytes: bytes
    raw_json: dict[str, Any]
    text: str
    response_id: str | None
    usage: dict[str, Any]
    cached_tokens: int | None
    response_output: list[Any]

    # V2 fields are additive so the legacy Fusion/Responses runtime remains
    # source-compatible. history_delta is provider-native state to append only
    # after a successful fenced commit.
    history_delta: Any = None
    finish_reason: str | None = None
    result_type: str = "message"
    wire_request_hash: str | None = None
    applied_generation: dict[str, Any] = field(default_factory=dict)
    provider_metadata: dict[str, Any] = field(default_factory=dict)


class ProviderRequestError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ProviderHTTPError(RuntimeError):
    """Lossless provider/dependency HTTP error captured at the HTTP boundary."""

    def __init__(
        self,
        status_code: int,
        body: bytes,
        message: str = "Upstream provider request failed",
        *,
        headers: list[tuple[str, str]] | None = None,
        content_type: str | None = None,
        content_encoding: str | None = None,
        received_complete: bool = True,
        provider: str | None = None,
        service: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.headers = list(headers or [])
        self.content_type = content_type
        self.content_encoding = content_encoding
        self.received_complete = received_complete
        self.provider = provider
        self.service = service


class ProviderTransportError(RuntimeError):
    """Transport failure where no complete HTTP response is available."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        service: str | None = None,
        exception_type: str | None = None,
        errno: int | None = None,
        cause_chain: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.service = service
        self.exception_type = exception_type or self.__class__.__name__
        self.errno = errno
        self.cause_chain = list(cause_chain or [])


class ProviderAdapter(Protocol):
    """Legacy adapter protocol kept for V1/Fusion compatibility."""

    async def execute(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any] | None,
        material_prefix: Any,
        history: list[dict[str, Any]],
    ) -> ProviderResult:
        ...


BeforeDispatch = Callable[[str], Awaitable[None]]


class V2ProviderAdapter(Protocol):
    """Provider-neutral V2 adapter contract.

    Core provides logical context/history/input and a MaterialResolver. Adapters
    own the provider wire protocol and call before_dispatch exactly once, after
    all material preparation but immediately before the model generation request.
    """

    provider: str
    protocol: str

    async def execute_v2(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any],
        context: list[dict[str, Any]],
        history: Any,
        material_resolver: Any,
        before_dispatch: BeforeDispatch,
    ) -> ProviderResult:
        ...

    def decode_archived_v2(
        self,
        request_snapshot: dict[str, Any],
        archived: dict[str, Any],
    ) -> ProviderResult:
        ...
