"""Compose the final live authority check inside the diagnostic transport.

Observability owns wire capture; the runtime owns permission to reach the
underlying HTTP transport, after headers and request bytes have been prepared.
"""
import httpx
import asyncio

from app.observability.http_wire_logger import provider_transport as diagnostic_transport, wire_phase
from app.runtime.execution_guard import current_execution_guard, ensure_execution_allowed

class FencedRequestStream(httpx.AsyncByteStream):
    def __init__(self,inner,states):self.inner,self.states=inner,states
    async def __aiter__(self):
        async for chunk in self.inner:
            await ensure_execution_allowed(states=self.states)
            yield chunk
        await ensure_execution_allowed(states=self.states)
    async def aclose(self):await self.inner.aclose()


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
        guard=current_execution_guard()
        if guard is None:return await self.wrapped.handle_async_request(request)
        request.stream=FencedRequestStream(request.stream,states)
        sender=asyncio.create_task(self.wrapped.handle_async_request(request))
        async def watch_authority():
            from app.runtime.execution_guard import current_cache_operation
            if current_cache_operation() is None:
                await guard.lost.wait()
                return
            while not guard.lost.is_set():
                try:await asyncio.wait_for(guard.lost.wait(),.5)
                except asyncio.TimeoutError:await ensure_execution_allowed(states=states)
        lost=asyncio.create_task(watch_authority())
        try:
            await asyncio.wait((sender,lost),return_when=asyncio.FIRST_COMPLETED)
            if guard.lost.is_set():
                from app.runtime.execution_guard import ExecutionLeaseLost
                sender.cancel()
                await asyncio.gather(sender,return_exceptions=True)
                raise ExecutionLeaseLost(guard.reason)
            return await sender
        finally:
            lost.cancel()
            if not sender.done():sender.cancel()
            await asyncio.gather(lost,sender,return_exceptions=True)

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
