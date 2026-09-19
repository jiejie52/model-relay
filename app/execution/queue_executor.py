from __future__ import annotations

import asyncio

from ..config import Settings
from ..core.execution_runtime import SharedExecutionRuntime
from ..core.raw_error import RawErrorRecorder
from ..v2_repository import RelayV2Repository
from .errors import error_source


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
        lease_epoch = int(job.get("lease_epoch") or 0)
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
                return
            await asyncio.wait_for(
                self.runtime.execute(
                    request_row,
                    lease_owner=worker_id,
                    lease_epoch=lease_epoch,
                ),
                timeout=self.settings.worker_max_runtime_seconds,
            )
        except asyncio.TimeoutError as exc:
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
