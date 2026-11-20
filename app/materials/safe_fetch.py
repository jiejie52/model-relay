from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

import httpx


class MaterialFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "MATERIAL_SOURCE_FETCH_FAILED",
        phase: str = "source_fetch",
        status_code: int | None = None,
        body: bytes | None = None,
        response_headers: dict[str, str] | None = None,
        bytes_received: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.status_code = status_code
        self.body = body
        self.response_headers = response_headers or {}
        self.bytes_received = bytes_received


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_public_url(url: str, *, allow_http: bool = False) -> None:
    parsed = urlparse(url)
    allowed = {"https"}
    if allow_http:
        allowed.add("http")
    if parsed.scheme.lower() not in allowed:
        raise MaterialFetchError(
            "material source_url must use an allowed HTTP scheme",
            code="MATERIAL_SOURCE_POLICY_REJECTED",
            phase="source_validate",
        )
    if not parsed.hostname:
        raise MaterialFetchError(
            "material source_url has no hostname",
            code="MATERIAL_SOURCE_POLICY_REJECTED",
            phase="source_validate",
        )

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM),
        )
    except OSError as exc:
        raise MaterialFetchError(
            f"material source hostname could not be resolved: {exc}",
            code="MATERIAL_SOURCE_DNS_FAILED",
            phase="source_dns",
        ) from exc
    addresses = {item[4][0] for item in infos}
    if not addresses or any(not _is_public_ip(ip) for ip in addresses):
        raise MaterialFetchError(
            "material source_url resolved to a non-public address",
            code="MATERIAL_SOURCE_POLICY_REJECTED",
            phase="source_validate",
        )


async def fetch_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    allow_http: bool = False,
) -> tuple[bytes, str | None]:
    await validate_public_url(url, allow_http=allow_http)
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 30.0))
    total = 0
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=True, follow_redirects=False) as client:
            async with client.stream("GET", url, headers={"Accept": "*/*"}) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raw = await response.aread()
                    raise MaterialFetchError(
                        f"material source returned HTTP {response.status_code}: "
                        + raw.decode("utf-8", errors="replace"),
                        code="MATERIAL_SOURCE_HTTP_ERROR",
                        phase="source_http",
                        status_code=response.status_code,
                        body=raw,
                        response_headers=dict(response.headers),
                        bytes_received=len(raw),
                    )
                chunks: list[bytes] = []
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise MaterialFetchError(
                            "material exceeds configured ingress size limit",
                            code="MATERIAL_SOURCE_TOO_LARGE",
                            phase="source_stream",
                            status_code=response.status_code,
                            response_headers=dict(response.headers),
                            bytes_received=total,
                        )
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type")
    except MaterialFetchError:
        raise
    except httpx.TimeoutException as exc:
        raise MaterialFetchError(
            f"material source request timed out: {exc}",
            code="MATERIAL_SOURCE_TIMEOUT",
            phase="source_fetch",
            bytes_received=total,
        ) from exc
    except httpx.TransportError as exc:
        raise MaterialFetchError(
            f"material source transport failed: {exc}",
            code="MATERIAL_SOURCE_TRANSPORT_ERROR",
            phase="source_fetch",
            bytes_received=total,
        ) from exc
    except Exception as exc:
        raise MaterialFetchError(
            f"material source fetch failed: {exc}",
            code="MATERIAL_SOURCE_FETCH_FAILED",
            phase="source_fetch",
            bytes_received=total,
        ) from exc
