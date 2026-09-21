from __future__ import annotations

import asyncio
import logging

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
        log_info(
            logger,
            "request_executor_started",
            request_id=request_id,
            session_id=session_id,
            execution_mode="sync",
            deadline_seconds=self.settings.sync_request_deadline_seconds,
        )
        try:
            await asyncio.wait_for(
                self.runtime.execute(request_row),
                timeout=self.settings.sync_request_deadline_seconds,
            )
        except asyncio.TimeoutError as exc:
            log_error(
                logger,
                "request_executor_timeout",
                exc_info=True,
                request_id=request_id,
                session_id=session_id,
                execution_mode="sync",
                duration_ms=elapsed_ms(started_ms),
                failure_class="upstream_timeout",
                exception_type=type(exc).__name__,
                provider_dispatch_state=request_row.get("provider_dispatch_state"),
            )
            err = await self.errors.record(
                exc=exc,
                source="relay_timeout",
                tenant_id=request_row["tenant_id"],
                conversation_hash=request_row["conversation_hash"],
                session_id=request_row["session_id"],
                request_id=request_row["id"],
            )
            # Provider side execution may already have happened. Do not create an
            # async job and do not pretend the provider did not receive it.
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
