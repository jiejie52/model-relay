from __future__ import annotations
import asyncio
import time
from uuid import uuid4
from app.core.idempotency import V3_FENCED_REQUEST_VERSIONS
from app.observability.response_capture import response_capture_scope, attach_response_evidence
from app.shared_contracts import ExecutionConflictError, ExecutionFence
from app.execution_plane.schedulers.common import (
    RequestExecutionService,
    is_transient_commit_failure,
    log_failure_observed,
)

_TERMINAL_REQUEST_STATUSES = frozenset({"succeeded", "failed", "cancelled", "indeterminate"})
_SYNC_COMMIT_RETRY_DELAYS = (0.5, 1.0)


class InlineScheduler(RequestExecutionService):
    async def _acquire_sync_fence(self, request_row, is_v3: bool, *, commit_only: bool = False):
        if not is_v3:
            return None
        owner = f"sync:{uuid4()}"
        acquire = (
            self.repo.acquire_sync_commit_fence
            if commit_only or request_row.get("provider_dispatch_state") == "result_stored"
            else self.repo.acquire_sync_request_fence
        )
        epoch = await acquire(
            str(request_row["id"]),
            owner,
            lease_seconds=int(self.settings.sync_request_deadline_seconds) + 30,
        )
        if epoch is None:
            return None
        return ExecutionFence(owner, epoch, kind="sync_request")

    async def _release_sync_commit_fence_safe(self, request_id, fence) -> None:
        if fence is None:
            return
        try:
            # The child execution has stopped. Release only a durable
            # result's commit right, never an in-flight inference right.
            await self.repo.release_sync_commit_fence(str(request_id), fence.owner, fence.epoch)
        except Exception:
            pass  # expiry still permits later commit-only recovery

    async def _safe_get_request(self, request_id):
        try:
            return await self.repo.get_request(request_id)
        except Exception:
            return None

    def _finalize_diagnostics(self, exc: Exception, diagnostics: dict) -> None:
        if isinstance(exc, TimeoutError):
            if diagnostics.get("_started") is not None:
                diagnostics["elapsed_ms"] = round((time.monotonic() - diagnostics["_started"]) * 1000)
            diagnostics.update(
                reason_code="sync_deadline_exceeded",
                deadline_seconds=self.settings.sync_request_deadline_seconds,
            )
        attach_response_evidence(exc, diagnostics)

    async def _handle_sync_failure(self, request_row, fence, exc, *, is_v3: bool, deadline_at: float) -> None:
        current = await self._safe_get_request(request_row["id"]) or request_row
        status = current.get("status")
        state = current.get("provider_dispatch_state", "not_sent")
        if status in _TERMINAL_REQUEST_STATUSES:
            return
        if state != "result_stored":
            await self.record_failure(current, fence, exc)
            return

        # Deterministic failure after result_stored (e.g. HTTP 400, schema/contract error):
        # never leave a sync request stuck in running.
        if not is_transient_commit_failure(exc):
            force = not isinstance(exc, ExecutionConflictError) or (
                not is_v3 or fence is not None
            )
            await self.record_failure(current, fence, exc, force_terminal_sync=force)
            return

        # Transient failure after result_stored: retry commit-only recovery within the
        # remaining sync deadline without ever re-invoking the provider.
        last_exc = exc
        await self._release_sync_commit_fence_safe(current["id"], fence)
        for delay in _SYNC_COMMIT_RETRY_DELAYS:
            remaining = deadline_at - time.monotonic()
            if remaining <= delay + 0.5:
                break
            log_failure_observed(
                current,
                dispatch_state=state,
                exc=last_exc,
                handling="retrying_sync_commit_after_transient_failure",
            )
            await asyncio.sleep(delay)
            retry_row = await self._safe_get_request(current["id"]) or current
            if retry_row.get("status") != "running" or retry_row.get("provider_dispatch_state") != "result_stored":
                return
            commit_fence = await self._acquire_sync_fence(retry_row, is_v3, commit_only=True)
            if is_v3 and commit_fence is None:
                return
            retry_diagnostics = {}
            try:
                with response_capture_scope(retry_diagnostics):
                    await asyncio.wait_for(
                        self.execute_guarded(retry_row, commit_fence),
                        max(0.5, deadline_at - time.monotonic()),
                    )
                return
            except Exception as retry_exc:
                self._finalize_diagnostics(retry_exc, retry_diagnostics)
                last_exc = retry_exc
                if not is_transient_commit_failure(retry_exc):
                    await self.record_failure(retry_row, commit_fence, retry_exc, force_terminal_sync=True)
                    return
            finally:
                await self._release_sync_commit_fence_safe(retry_row["id"], commit_fence)

        final_row = await self._safe_get_request(current["id"]) or current
        if final_row.get("status") != "running" or final_row.get("provider_dispatch_state") != "result_stored":
            return
        fail_fence = await self._acquire_sync_fence(final_row, is_v3, commit_only=True)
        if is_v3 and fail_fence is None:
            log_failure_observed(
                final_row,
                dispatch_state="result_stored",
                exc=last_exc,
                handling="preserved_result_stored_for_commit_recovery",
            )
            return
        try:
            await self.record_failure(final_row, fail_fence, last_exc, force_terminal_sync=True)
        finally:
            await self._release_sync_commit_fence_safe(final_row["id"], fail_fence)

    async def execute(self, request_row):
        is_v3 = request_row.get("request_identity_version") in V3_FENCED_REQUEST_VERSIONS
        deadline_seconds = float(self.settings.sync_request_deadline_seconds)
        deadline_at = time.monotonic() + deadline_seconds
        fence = await self._acquire_sync_fence(request_row, is_v3)
        if is_v3 and fence is None:
            return await self.repo.get_request(request_row["id"])
        diagnostics = {}
        try:
            # Child tasks share this request-local evidence dictionary. wait_for
            # replaces CancelledError with TimeoutError, but cannot erase it.
            with response_capture_scope(diagnostics):
                await asyncio.wait_for(self.execute_guarded(request_row, fence), deadline_seconds)
        except Exception as exc:
            self._finalize_diagnostics(exc, diagnostics)
            await self._handle_sync_failure(request_row, fence, exc, is_v3=is_v3, deadline_at=deadline_at)
        finally:
            await self._release_sync_commit_fence_safe(request_row["id"], fence)
        return await self.repo.get_request(request_row["id"])

    execute_sync = execute

