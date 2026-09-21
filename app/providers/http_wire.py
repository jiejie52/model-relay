from __future__ import annotations

import gzip
import logging
import zlib
from typing import Any
from urllib.parse import urlsplit

from ..observability import error as log_error


logger = logging.getLogger("model-relay-upstream")


async def read_raw_response(response, *, log_context: dict[str, Any] | None = None) -> bytes:
    """Read a provider response body and make mid-stream disconnects explicit.

    Normal successful chunks are deliberately not logged.  If the provider
    closes/resets the stream after headers were received, the log records the
    response status and number of bytes already received before re-raising the
    original exception so the Raw Error path remains authoritative.
    """

    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_raw():
            total += len(chunk)
            chunks.append(chunk)
    except Exception as exc:
        headers = getattr(response, "headers", {}) or {}
        request = getattr(response, "request", None)
        method = getattr(request, "method", None)
        url = str(getattr(request, "url", "") or "")
        parsed = urlsplit(url) if url else None
        upstream_request_id = None
        for key in ("x-request-id", "request-id", "x-goog-request-id", "x-moonshot-request-id"):
            value = headers.get(key)
            if value:
                upstream_request_id = str(value)
                break
        fields: dict[str, Any] = {
            "phase": "response_stream_read",
            "http_status": getattr(response, "status_code", None),
            "bytes_received": total,
            "content_length": headers.get("content-length"),
            "upstream_request_id": upstream_request_id,
            "method": method,
            "upstream_host": parsed.hostname if parsed else None,
            "upstream_path": parsed.path if parsed else None,
            "stream_interrupted": True,
            "failure_class": "upstream_transport",
            "exception_type": type(exc).__name__,
        }
        fields.update(log_context or {})
        log_error(logger, "upstream_stream_interrupted", exc_info=True, **fields)
        try:
            setattr(exc, "stream_interrupted", True)
            setattr(exc, "bytes_received", total)
            if getattr(exc, "status_code", None) is None:
                setattr(exc, "status_code", getattr(response, "status_code", None))
        except Exception:
            pass
        raise
    return b"".join(chunks)


def decode_entity(raw: bytes, content_encoding: str | None) -> bytes:
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
    # We request identity, so an unexpected encoding is intentionally not
    # guessed. The raw bytes remain available to the Error channel.
    raise ValueError(f"Unsupported Content-Encoding from provider: {encoding}")
