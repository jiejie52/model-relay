from dataclasses import dataclass
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


class ProviderRequestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProviderHTTPError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        body: bytes,
        message: str = "Upstream provider request failed",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ProviderAdapter(Protocol):
    async def execute(
        self,
        request_snapshot: dict[str, Any],
        *,
        session: dict[str, Any] | None,
        material_prefix: Any,
        history: list[dict[str, Any]],
    ) -> ProviderResult:
        ...
