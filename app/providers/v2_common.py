from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from datetime import datetime
from typing import Any

import httpx

from .base import ProviderHTTPError, ProviderTransportError


def wire_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def send_raw(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> tuple[httpx.Response, bytes]:
    """Send an HTTP request and capture the body before httpx content decoding."""
    request = client.build_request(method, url, **kwargs)
    response = await client.send(request, stream=True)
    chunks: list[bytes] = []
    try:
        async for chunk in response.aiter_raw():
            chunks.append(chunk)
    finally:
        await response.aclose()
    return response, b"".join(chunks)


def decode_http_body(raw: bytes, content_encoding: str | None) -> bytes:
    encoding = str(content_encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return raw
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    # Brotli/other codings remain losslessly archived, but the current provider
    # result parser cannot safely decode them without an explicitly registered
    # codec. Providers are requested with Accept-Encoding: identity.
    raise ValueError(f"Unsupported Content-Encoding for provider success response: {encoding}")


def response_http_error(response: httpx.Response, raw: bytes, *, provider: str, service: str) -> ProviderHTTPError:
    return ProviderHTTPError(
        response.status_code,
        raw,
        headers=list(response.headers.multi_items()),
        content_type=response.headers.get("content-type"),
        content_encoding=response.headers.get("content-encoding"),
        received_complete=True,
        provider=provider,
        service=service,
    )


def transport_error(exc: Exception, *, provider: str, service: str) -> ProviderTransportError:
    errno = getattr(exc, "errno", None)
    chain: list[str] = []
    current = exc.__cause__
    while current is not None and len(chain) < 8:
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__
    return ProviderTransportError(
        str(exc),
        provider=provider,
        service=service,
        exception_type=type(exc).__name__,
        errno=errno if isinstance(errno, int) else None,
        cause_chain=chain,
    )


def parse_expiry(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
