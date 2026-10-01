from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from ..config import Settings
from ..core.execution_runtime import SharedExecutionRuntime
from ..core.raw_error import RawErrorRecorder
from ..observability import elapsed_ms, error as log_error, info as log_info, now_ms, exception_failure_class
from ..v2_repository import RelayV2Repository
from .errors import error_source


logger = logging.getLogger("model-relay-execution")


class InlineExecutor:
    def __init__(
        self,
        runtime: SharedExecutionRuntime,
        errors: RawErrorRecorder,
        repo: RelayV2Repository,
        settings: Settings,
    ) -> None:
        self.runtime = runtime
        self.errors = errors
        self.repo = repo
        self.settings = settings

    async def execute(self, request_row: dict):
        started_ms = now_ms()
        request_id = request_row.get("id")
        session_id = request_row.get("session_id")
        is_v3 = str(request_row.get("request_identity_version") or "") == "relay-request/2.3"
        executor_id: str | None = None
        executor_epoch: int | None = None
        if is_v3:
            executor_id = f"sync:{uuid4()}"
            executor_epoch = await self.repo.acquire_sync_request_fence(
                str(request_id),
                executor_id,
                lease_seconds=int(self.settings.sync_request_deadline_seconds) + 30,
            )
            if executor_epoch is None:
                # Another executor/recovery path owns the Request. A stale API
                # process must not manufacture a new sending right.
                return await self.repo.get_request(request_row["id"])

        log_info(
            logger,
            "request_executor_started",
            request_id=request_id,
            session_id=session_id,
            execution_mode="sync",
            deadline_seconds=self.settings.sync_request_deadline_seconds,
            executor_id=executor_id,
            executor_epoch=executor_epoch,
        )
        try:
            await asyncio.wait_for(
                self.runtime.execute(
                    request_row,
                    lease_owner=executor_id,
                    lease_epoch=executor_epoch,
                ),
                timeout=self.settings.sync_request_deadline_seconds,
            )
        except asyncio.TimeoutError as exc:
            current = await self.repo.get_request(request_row["id"]) or request_row
            dispatch_state = str(current.get("provider_dispatch_state") or "not_sent")
            after_dispatch = dispatch_state != "not_sent"
            status = "indeterminate" if after_dispatch else "failed"
            release_session = not after_dispatch
            log_error(
                logger,
                "request_executor_timeout",
                exc_info=True,
                request_id=request_id,
                session_id=session_id,
                execution_mode="sync",
                duration_ms=elapsed_ms(started_ms),
                failure_class="upstream_timeout" if after_dispatch else "relay_timeout",
                exception_type=type(exc).__name__,
                provider_dispatch_state=dispatch_state,
            )
            err = await self.errors.record(
                exc=exc,
                source="relay_timeout",
                tenant_id=request_row["tenant_id"],
                conversation_hash=request_row["conversation_hash"],
                session_id=request_row["session_id"],
                request_id=request_row["id"],
            )
            if is_v3 and executor_id is not None and executor_epoch is not None:
                await self.repo.fail_request_v3(
                    request_id=request_row["id"],
                    session_id=request_row["session_id"],
                    error=err,
                    status=status,
                    release_session=release_session,
                    fence_owner=executor_id,
                    fence_epoch=executor_epoch,
                )
            else:
                # Historical sync semantics are intentionally unchanged.
                await self.repo.fail_request(
                    request_id=request_row["id"],
                    session_id=request_row["session_id"],
                    error=err,
                    status="indeterminate",
                    release_session=False,
                )
        except Exception as exc:
            log_error(
                logger,
                "request_executor_failed",
                exc_info=(error_source(exc) != "provider"),
                request_id=request_id,
                session_id=session_id,
                execution_mode="sync",
                duration_ms=elapsed_ms(started_ms),
                failure_class=exception_failure_class(exc),
                error_source=error_source(exc),
                exception_type=type(exc).__name__,
                upstream_http_status=getattr(exc, "status_code", None),
                upstream_request_id=getattr(exc, "request_id", None),
                upstream_phase=getattr(exc, "phase", None),
            )
            err = await self.errors.record(
                exc=exc,
                source=error_source(exc),
                tenant_id=request_row["tenant_id"],
                conversation_hash=request_row["conversation_hash"],
                session_id=request_row["session_id"],
                request_id=request_row["id"],
            )
            if is_v3 and executor_id is not None and executor_epoch is not None:
                await self.repo.fail_request_v3(
                    request_id=request_row["id"],
                    session_id=request_row["session_id"],
                    error=err,
                    status="failed",
                    release_session=True,
                    fence_owner=executor_id,
                    fence_epoch=executor_epoch,
                )
            else:
                await self.repo.fail_request(
                    request_id=request_row["id"],
                    session_id=request_row["session_id"],
                    error=err,
                    status="failed",
                    release_session=True,
                )
        row = await self.repo.get_request(request_row["id"])
        log_info(
            logger,
            "request_executor_finished",
            request_id=request_id,
            session_id=session_id,
            execution_mode="sync",
            duration_ms=elapsed_ms(started_ms),
            status=row.get("status") if row else None,
        )
        return row

