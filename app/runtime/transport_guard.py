"""Compose the final live authority check inside the diagnostic transport.

Observability owns wire capture; the runtime owns permission to reach the
underlying HTTP transport, after headers and request bytes have been prepared.
"""
import httpx

from app.observability.http_wire_logger import provider_transport as diagnostic_transport, wire_phase
from app.runtime.execution_guard import current_execution_guard, ensure_execution_allowed


class ExecutionFencedAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, wrapped):
        self.wrapped = wrapped

    async def handle_async_request(self, request):
        states = ("dispatch_started",) if wire_phase() == "inference" else ("not_sent",)
        await ensure_execution_allowed(states=states)
        return await self.wrapped.handle_async_request(request)

    async def aclose(self):
        await self.wrapped.aclose()


def provider_transport(settings, provider, wrapped=None, url=None, *, always=False):
    transport = diagnostic_transport(settings, provider, wrapped, url,
        always=always or current_execution_guard() is not None)
    if transport is not None:
        transport.wrapped = ExecutionFencedAsyncTransport(transport.wrapped)
    return transport
