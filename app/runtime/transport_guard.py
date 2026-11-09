"""Compose the final live authority check inside the diagnostic transport.

Observability owns wire capture; the runtime owns permission to reach the
underlying HTTP transport, after headers and request bytes have been prepared.
"""
import httpx

from app.observability.http_wire_logger import provider_transport as diagnostic_transport, wire_phase
from app.runtime.execution_guard import current_execution_guard, ensure_execution_allowed


class ExecutionFencedAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, wrapped, connection=None):
        self.wrapped, self.connection = wrapped, connection

    async def handle_async_request(self, request):
        if self.connection is not None:
            if self.connection.spec.upload_base_url and request.url.path.endswith("/files") and "/upload/" in request.url.path:
                base = self.connection.spec.upload_base_url.rstrip("/")
                upload = base if base.endswith("/files") else base+"/files"
                request.url = httpx.URL(upload).copy_with(query=request.url.query)
            from urllib.parse import urlsplit
            if request.url.scheme != "https" or request.url.host not in self.connection.allowed_hosts():
                raise ValueError("CONNECTION_HOST_FORBIDDEN")
        states = ("dispatch_started",) if wire_phase() == "inference" else ("not_sent",)
        await ensure_execution_allowed(states=states)
        return await self.wrapped.handle_async_request(request)

    async def aclose(self):
        await self.wrapped.aclose()


class BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, pool): self.pool = pool
    async def handle_async_request(self, request): return await self.pool.handle_async_request(request)
    async def aclose(self): pass  # ConnectionAssembly owns the pool.

def provider_transport(settings, provider, wrapped=None, url=None, *, always=False, connection=None):
    from app.runtime.connection_scope import current_client
    connection = connection or current_client()
    if connection is not None and wrapped is None: wrapped = BorrowedTransport(connection.pool)
    transport = diagnostic_transport(settings, provider, wrapped, url, always=True)
    transport.wrapped = ExecutionFencedAsyncTransport(transport.wrapped, connection)
    return transport
