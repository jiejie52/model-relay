"""Provider HTTP boundary: affinity, exact entity observations, and opt-in diagnostics."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx

from ..core.cache_fingerprint import compute_lcp_diff, physical_chain
from ..core.context_normalizer import canonical_structure, normalize_native_payload, serialize_wire
from . import info as log_info, warning as log_warning

logger = logging.getLogger("relay.http.wire")
_current: ContextVar[dict[str, Any] | None] = ContextVar("relay_wire_context", default=None)
_ROUTER_HEADERS = (
    "x-aihubmix-request-id", "x-aihubmix-router-sticky", "x-aihubmix-router-resolved-model",
    "x-aihubmix-router-fallback", "x-aihubmix-router-reason",
)


@contextmanager
def wire_context(context: dict[str, Any]):
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


def affinity_hint() -> str | None:
    return (_current.get() or {}).get("affinity_token")


def normalization_active() -> bool:
    return (_current.get() or {}).get("normalize") is True


def _headers(headers: httpx.Headers, redact: bool) -> list[tuple[str, str]]:
    entries = []
    for name, value in headers.multi_items():
        if redact and (
            name in {"authorization", "proxy-authorization", "cookie", "set-cookie", "location"}
            or any(word in name for word in ("api-key", "apikey", "token", "secret", "upload-url"))
        ):
            value = f"{value[:4]}…{value[-4:]}" if len(value) >= 16 else "[redacted]"
        entries.append((name, value))
    return entries


class WireLoggingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, wrapped: httpx.AsyncBaseTransport, settings: Any, provider: str):
        self.wrapped = wrapped
        self.settings = settings
        self.provider = provider

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ctx = _current.get() or {}
        affinity = ctx.get("affinity_token")
        if affinity and request.url.host in (ctx.get("affinity_hosts") or ()):
            request.headers[ctx["affinity_header"]] = affinity
            if ctx.get("secondary_affinity_header"):
                request.headers[ctx["secondary_affinity_header"]] = affinity

        enabled = self.settings.relay_http_wire_log_enabled
        filters = {item.strip().lower() for item in self.settings.relay_http_wire_log_providers.split(",")}
        enabled = enabled and bool(filters & {"*", self.provider, str(ctx.get("provider") or ""), str(ctx.get("channel") or "")})
        body = None
        if ctx.get("normalize") and "application/json" in request.headers.get("content-type", ""):
            body = await request.aread()
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                payload = None
            if isinstance(payload, dict):
                payload = normalize_native_payload(payload, unordered_tools=ctx.get("unordered_tools") is True)
                if self.provider == "gemini" and "systemInstruction" in payload:
                    # Canonicalize every object first, then preserve Gemini's
                    # stable instruction as the first top-level wire field.
                    ordered = canonical_structure(payload)
                    ordered = {
                        "systemInstruction": ordered["systemInstruction"],
                        **{key: value for key, value in ordered.items() if key != "systemInstruction"},
                    }
                    body = json.dumps(
                        ordered, ensure_ascii=False, sort_keys=False, separators=(",", ":"),
                    ).encode("utf-8")
                else:
                    body = serialize_wire(payload)
                request.headers.pop("transfer-encoding", None)
                request.headers["content-length"] = str(len(body))
                request = httpx.Request(request.method, request.url, headers=request.headers,
                                        content=body, extensions=request.extensions)
                if ctx.get("phase") == "inference":
                    chain = physical_chain(payload, scope=ctx["scope"])
                    ctx["physical_chain"] = chain
                    ctx["prefix_diff"] = compute_lcp_diff(ctx.get("previous_chain") or [], chain)
                    ctx["payload_sha256"] = hashlib.sha256(body).hexdigest()
                    if (ctx.get("previous_semantic_fingerprint") and
                            ctx["previous_semantic_fingerprint"] == ctx.get("semantic_fingerprint") and
                            ctx["prefix_diff"]["first_divergence_index"] is not None):
                        ctx["prefix_diff"]["divergence_reason"] = "wire_projection_changed"
        if enabled:
            # Provider JSON requests are replayable byte streams. Avoid consuming streaming file uploads.
            if body is None and isinstance(request.stream, httpx.ByteStream):
                body = await request.aread()
            self._emit("outbound", request, request.headers, body, ctx)
        response = await self.wrapped.handle_async_request(request)
        if ctx.get("phase") == "inference":
            ctx["route_observation"] = {name: response.headers[name] for name in _ROUTER_HEADERS if name in response.headers}
        if enabled:
            self._emit("inbound_headers", request, response.headers, None, ctx, response.status_code)
            if not response.is_stream_consumed:
                response.stream = _ResponseCapture(response.stream, self, request, response, ctx)
        return response

    def _emit(self, direction: str, request: httpx.Request, headers: httpx.Headers,
              body: bytes | None, ctx: dict[str, Any], status: int | None = None,
              *, complete: bool = True, dump_path: str | None = None,
              digest: str | None = None, size: int | None = None,
              head: bytes | None = None, tail: bytes | None = None) -> None:
        try:
            limit = self.settings.relay_http_wire_log_max_body_bytes
            facts: dict[str, Any] = {}
            if body is not None:
                facts = {"body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body)}
                if len(body) <= limit:
                    facts["body_hex"] = body.hex()
                else:
                    fd, dump_path = _open_dump(self.settings.relay_http_wire_log_directory)
                    with os.fdopen(fd, "wb") as target:
                        target.write(body)
                    facts["body_head_hex"] = body[:256].hex()
                    facts["body_tail_hex"] = body[-256:].hex()
            if digest is not None:
                facts.update(body_sha256=digest, body_bytes=size)
            if dump_path and head is not None and tail is not None:
                facts.update(body_head_hex=head.hex(), body_tail_hex=tail.hex())
            log_info(logger, "provider_http_wire", direction=direction, method=request.method,
                     url=str(request.url.copy_with(query=None, username=None, password=None)),
                     headers=_headers(headers, self.settings.relay_http_wire_log_redact_auth),
                     status=status, body_complete=complete, wire_dump_path=dump_path,
                     request_id=ctx.get("request_id"), session_id=ctx.get("session_id"),
                     phase=ctx.get("phase"), **(ctx.get("prefix_diff") or {}), **facts)
        except Exception as exc:
            log_warning(logger, "provider_http_wire_logging_failed", error_type=type(exc).__name__)

    async def aclose(self) -> None:
        await self.wrapped.aclose()


def _open_dump(directory: str) -> tuple[int, str]:
    Path(directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    return tempfile.mkstemp(prefix="wire-", suffix=".bin", dir=directory)


class _ResponseCapture(httpx.AsyncByteStream):
    def __init__(self, inner, transport, request, response, ctx):
        self.inner, self.transport, self.request, self.response, self.ctx = inner, transport, request, response, ctx

    async def __aiter__(self):
        buffer = bytearray()
        digest = hashlib.sha256()
        size = 0
        head, tail = b"", b""
        target = None
        path = None
        capture = True
        complete = False
        try:
            async for chunk in self.inner:
                if capture:
                    try:
                        digest.update(chunk)
                        size += len(chunk)
                        head = (head + chunk[:256])[:256]
                        tail = (tail + chunk[-256:])[-256:]
                        if target is None and size > self.transport.settings.relay_http_wire_log_max_body_bytes:
                            fd, path = _open_dump(self.transport.settings.relay_http_wire_log_directory)
                            target = os.fdopen(fd, "wb")
                            target.write(buffer)
                            buffer.clear()
                        if target is None:
                            buffer.extend(chunk)
                        else:
                            target.write(chunk)
                    except OSError:
                        capture = False
                yield chunk
            complete = True
        finally:
            if target is not None:
                try:
                    target.close()
                except OSError:
                    capture = False
            self.transport._emit("inbound_body", self.request, self.response.headers,
                                 bytes(buffer) if target is None and capture else None,
                                 self.ctx, self.response.status_code, complete=complete and capture,
                                 dump_path=path, digest=digest.hexdigest(), size=size, head=head, tail=tail)

    async def aclose(self):
        await self.inner.aclose()


def provider_transport(settings: Any, provider: str, wrapped=None, url: str | None = None, *, always: bool = False):
    if wrapped is None and not always and not settings.relay_http_wire_log_enabled and not _current.get():
        return None
    if wrapped is None:
        # Explicit transport bypasses HTTPX automatic environment proxy mounting.
        # Preserve it for this Provider endpoint, without affecting storage clients.
        from httpx._utils import URLPattern, get_environment_proxies

        parsed_url = httpx.URL(url) if url else None
        proxy = None
        if parsed_url is not None:
            matches = sorted((URLPattern(pattern), value) for pattern, value in get_environment_proxies().items())
            proxy = next((value for pattern, value in matches if pattern.matches(parsed_url)), None)
        wrapped = httpx.AsyncHTTPTransport(proxy=proxy)
    return WireLoggingAsyncTransport(wrapped, settings, provider)
