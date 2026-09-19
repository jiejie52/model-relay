from __future__ import annotations

import gzip
import zlib


async def read_raw_response(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.aiter_raw():
        chunks.append(chunk)
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
