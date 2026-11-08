"""Live execution authority, kept outside immutable request identity and wire data."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

from app.shared_contracts import ExecutionConflictError


class ExecutionLeaseLost(ExecutionConflictError):
    """Authority was lost or cannot be confirmed; never authorize another send."""


_active_guard: ContextVar[ExecutionGuard | None] = ContextVar("relay_execution_guard", default=None)
_active_operation: ContextVar[dict | None] = ContextVar("relay_cache_operation_guard", default=None)


class ExecutionGuard:
    def __init__(self, repo, row, fence, settings):
        self.repo, self.row, self.fence, self.settings = repo, row, fence, settings
        self.lost = asyncio.Event()
        self.reason = "Execution lease is unavailable"
        self.allowed_states = ("not_sent", "dispatch_started", "result_stored")
        self.check_timeout = max(0.05, min(5.0, float(settings.job_lease_seconds) / 4))

    def invalidate(self, reason):
        self.reason = reason
        self.lost.set()

    async def verify(self, *, states=None):
        if self.lost.is_set():
            raise ExecutionLeaseLost(self.reason)
        try:
            ok = await asyncio.wait_for(self.repo.verify_execution_fence(
                request_id=str(self.row["id"]), fence_owner=self.fence.owner,
                fence_epoch=self.fence.epoch, allowed_states=list(states or self.allowed_states)), self.check_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.invalidate("Execution lease cannot be verified")
            raise ExecutionLeaseLost(self.reason) from exc
        if not ok or self.lost.is_set():
            self.invalidate("Execution lease expired, changed owner, or the Request stopped")
            raise ExecutionLeaseLost(self.reason)

    async def heartbeat(self):
        lease = (self.settings.job_lease_seconds if self.row.get("job_id")
                 else self.settings.sync_request_deadline_seconds + 30)
        interval = max(0.05, min(float(self.settings.job_heartbeat_seconds), float(lease) / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                if self.row.get("job_id"):
                    renewed = self.repo.renew_lease_v2(str(self.row["job_id"]), self.fence.owner, self.fence.epoch)
                else:
                    renewed = self.repo.renew_sync_request_fence(str(self.row["id"]), self.fence.owner,
                        self.fence.epoch, lease_seconds=int(lease))
                ok = await asyncio.wait_for(renewed, min(self.check_timeout, interval))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.invalidate("Heartbeat renewal failed or timed out")
                return
            if not ok:
                self.invalidate("Heartbeat renewal was rejected")
                return


@contextmanager
def execution_guard(guard):
    token = _active_guard.set(guard)
    try:
        yield guard
    finally:
        _active_guard.reset(token)


def current_execution_guard():
    return _active_guard.get()


@contextmanager
def cache_operation_guard(operation):
    token = _active_operation.set(operation)
    try:
        yield
    finally:
        _active_operation.reset(token)


async def ensure_execution_allowed(*, states=None):
    guard = current_execution_guard()
    if guard is not None:
        await guard.verify(states=states)
        operation = _active_operation.get()
        if operation is not None:
            try:
                ok = await asyncio.wait_for(guard.repo.verify_cache_operation_fence(
                    operation_id=str(operation["op_id"]), lease_owner=operation["lease_owner"],
                    lease_epoch=operation["lease_epoch"]), guard.check_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                guard.invalidate("Cache operation authority cannot be confirmed")
                raise ExecutionLeaseLost(guard.reason) from exc
            if not ok:
                guard.invalidate("Cache operation lease expired or changed owner")
                raise ExecutionLeaseLost(guard.reason)
