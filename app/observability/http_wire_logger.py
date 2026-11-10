"""Provider HTTP boundary: affinity, exact entity observations, and opt-in diagnostics."""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx

from app.core.cache_fingerprint import compute_lcp_diff, physical_chain
from app.core.context_codec import normalize_native_payload, serialize_wire
from app.observability import info as log_info, warning as log_warning

logger = logging.getLogger("relay.http.wire")
_current: ContextVar[dict[str, Any] | None] = ContextVar("relay_wire_context", default=None)
_ROUTER_HEADERS = (
    "x-aihubmix-request-id", "x-aihubmix-router-sticky", "x-aihubmix-router-resolved-model",
    "x-aihubmix-router-fallback", "x-aihubmix-router-reason",
)
SENSITIVE_QUERY_KEYS={'key','api_key','apikey','access_token','sig','signature','token','upload_id','x-goog-signature','x-goog-credential','x-amz-signature','x-amz-credential'}

def sanitize_wire_url(raw_url, *, drop_query=False):
    from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
    parts=urlsplit(str(raw_url))
    host=parts.hostname or ''
    if ':' in host:host='['+host+']'
    if parts.port:host+=':'+str(parts.port)
    query='' if drop_query else urlencode([(k,'[redacted]' if k.lower() in SENSITIVE_QUERY_KEYS else v) for k,v in parse_qsl(parts.query,keep_blank_values=True)])
    return urlunsplit((parts.scheme,host,parts.path,query,''))


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


def wire_phase() -> str | None:
    return (_current.get() or {}).get("phase")


def _headers(headers: httpx.Headers, redact: bool) -> list[tuple[str, str]]:
    entries = []
    for name, value in headers.multi_items():
        if (
            name in {"authorization", "proxy-authorization", "cookie", "set-cookie", "location"}
            or any(word in name for word in ("api-key", "apikey", "token", "secret", "upload-url"))
        ):
            value = "[redacted]"
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
                payload = normalize_native_payload(payload, unordered_tools=ctx.get("unordered_tools") is True,
                                                   response_schemas=int(ctx.get("fingerprint_version") or 1) >= 2)
                # Native Gemini requires its system field to lead the top level.
                # Nested objects and every other top-level field remain canonical.
                body = serialize_wire(payload, system_instruction_first=True)
                request.headers.pop("transfer-encoding", None)
                request.headers["content-length"] = str(len(body))
                request = httpx.Request(request.method, request.url, headers=request.headers,
                                        content=body, extensions=request.extensions)
                if ctx.get("phase") == "inference":
                    chain = physical_chain(payload, scope=ctx["scope"],
                                           version=int(ctx.get("fingerprint_version") or 1),
                                           material_identities=ctx.get("material_identities"))
                    ctx["physical_chain"] = chain
                    ctx["prefix_diff"] = compute_lcp_diff(ctx.get("previous_chain") or [], chain)
                    ctx["payload_sha256"] = hashlib.sha256(body).hexdigest()
                    if (int(ctx.get("fingerprint_version") or 1) < 2 and ctx.get("previous_semantic_fingerprint") and
                            ctx["previous_semantic_fingerprint"] == ctx.get("semantic_fingerprint") and
                            ctx["prefix_diff"]["first_divergence_index"] is not None):
                        ctx["prefix_diff"]["divergence_reason"] = "wire_projection_changed"
        if enabled:
            # Provider JSON requests are replayable byte streams. Avoid consuming streaming file uploads.
            if body is None and isinstance(request.stream, httpx.ByteStream):
                body = await request.aread()
            self._emit("outbound", request, request.headers, body, ctx)
        trace = None
        if enabled:
            # HTTPX/httpcore emit these before a response is available. Do not
            # log trace arguments: they may contain addresses or TLS details.
            trace = {"observed": False, "tcp_connect_started": False, "tls_handshake_started": False}
            previous_trace = request.extensions.get("trace")

            async def trace_connection(name, info):
                trace["observed"] = True
                if name == "connection.connect_tcp.started":
                    trace["tcp_connect_started"] = True
                elif name == "connection.start_tls.started":
                    trace["tls_handshake_started"] = True
                if previous_trace is not None:
                    result = previous_trace(name, info)
                    if inspect.isawaitable(result):
                        await result

            request.extensions["trace"] = trace_connection
        response = None
        error_type = None
        try:
            response = await self.wrapped.handle_async_request(request)
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            if trace is not None:
                self._emit_connection_diagnostic(request, response, ctx, trace, error_type)
        if ctx.get("phase") == "inference":
            ctx["route_observation"] = {name: response.headers[name] for name in _ROUTER_HEADERS if name in response.headers}
        if enabled:
            self._emit("inbound_headers", request, response.headers, None, ctx, response.status_code)
            if not response.is_stream_consumed:
                response.stream = _ResponseCapture(response.stream, self, request, response, ctx)
        return response

    def _emit_connection_diagnostic(self, request, response, ctx, trace, error_type):
        try:
            from app.runtime.connection_scope import current_client
            connection = current_client()
            log_info(
                logger, "provider_http_connection",
                request_id=ctx.get("request_id"), session_id=ctx.get("session_id"),
                connection_id=getattr(connection, "connection_id", None),
                provider=self.provider, phase=ctx.get("phase"), process_id=os.getpid(),
                host=request.url.host, http_version=response.http_version if response else None,
                trace_observed=trace["observed"],
                tcp_connect_started=trace["tcp_connect_started"] if trace["observed"] else None,
                tls_handshake_started=trace["tls_handshake_started"] if trace["observed"] else None,
                error_type=error_type,
            )
        except Exception as exc:
            log_warning(logger, "provider_http_connection_logging_failed", error_type=type(exc).__name__)

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
                     url=sanitize_wire_url(request.url,drop_query=True),
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
