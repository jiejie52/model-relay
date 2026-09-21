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


class QueueExecutor:
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

    async def execute(self, job: dict, worker_id: str) -> None:
        request_id = job.get("request_id")
        if not request_id:
            raise ValueError("v2 async job is missing request_id")
        request_row = await self.repo.get_request(request_id)
        if not request_row or request_row.get("status") == "cancelled":
            return
        started_ms = now_ms()
        lease_epoch = int(job.get("lease_epoch") or 0)
        log_info(
            logger,
            "request_executor_started",
            request_id=request_id,
            session_id=request_row.get("session_id"),
            job_id=job.get("id"),
            execution_mode="async",
            worker_id=worker_id,
            lease_epoch=lease_epoch,
        )
        await self.repo.update_request(request_id, {"status": "running"})
        try:
            if request_row.get("provider_dispatch_state") == "result_stored":
                ok = await self.repo.complete_request(
                    request_id=request_row["id"],
                    session_id=request_row["session_id"],
                    history_object_id=request_row.get("provisional_history_object_id"),
                    result_object_id=request_row["provisional_result_object_id"],
                    output_object_id=request_row.get("provisional_output_object_id"),
                    compact_result=request_row.get("provisional_compact_result") or {},
                    provider_response_id=request_row.get("provider_response_id"),
                    expected_history_version=int(request_row["expected_history_version"]),
                    lease_owner=worker_id,
                    lease_epoch=lease_epoch,
                )
                if not ok:
                    raise RuntimeError("Stored provider result could not be atomically committed")
                log_info(
                    logger,
                    "request_commit_recovered",
                    request_id=request_id,
                    session_id=request_row.get("session_id"),
                    job_id=job.get("id"),
                    duration_ms=elapsed_ms(started_ms),
                )
                return
            await asyncio.wait_for(
                self.runtime.execute(
                    request_row,
                    lease_owner=worker_id,
                    lease_epoch=lease_epoch,
                ),
                timeout=self.settings.worker_max_runtime_seconds,
            )
            log_info(
                logger,
                "request_executor_finished",
                request_id=request_id,
                session_id=request_row.get("session_id"),
                job_id=job.get("id"),
                execution_mode="async",
                status="succeeded",
                duration_ms=elapsed_ms(started_ms),
            )
        except asyncio.TimeoutError as exc:
            log_error(
                logger,
                "request_executor_timeout",
                exc_info=True,
                request_id=request_id,
                session_id=request_row.get("session_id"),
                job_id=job.get("id"),
                execution_mode="async",
                duration_ms=elapsed_ms(started_ms),
                failure_class="upstream_timeout",
                exception_type=type(exc).__name__,
            )
            err = await self.errors.record(
                exc=exc,
                source="relay_timeout",
                tenant_id=request_row["tenant_id"],
                conversation_hash=request_row["conversation_hash"],
                session_id=request_row["session_id"],
                request_id=request_row["id"],
            )
            await self.repo.fail_request(
                request_id=request_row["id"],
                session_id=request_row["session_id"],
                error=err,
                status="indeterminate",
                release_session=False,
                lease_owner=worker_id,
                lease_epoch=lease_epoch,
            )
        except Exception as exc:
            log_error(
                logger,
                "request_executor_failed",
                exc_info=(error_source(exc) != "provider"),
                request_id=request_id,
                session_id=request_row.get("session_id"),
                job_id=job.get("id"),
                execution_mode="async",
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
                lease_owner=worker_id,
                lease_epoch=lease_epoch,
            )
