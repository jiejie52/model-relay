from __future__ import annotations
import asyncio
from app.shared_contracts import ExecutionFence
from app.execution_plane.schedulers.common import RequestExecutionService

class QueueScheduler(RequestExecutionService):
    async def execute(self, job, worker_id):
        if not job.get("request_id"): raise ValueError("v2 async job is missing request_id")
        row = await self.repo.get_request(job["request_id"])
        if not row or row.get("status") in {"cancelled", "failed", "succeeded", "indeterminate"}: return
        fence = ExecutionFence(worker_id, int(job.get("lease_epoch") or 0), kind="async_job")
        if not await self.repo.start_request_execution(request_id=str(row["id"]),
                fence_owner=fence.owner, fence_epoch=fence.epoch):
            return
        try:
            await asyncio.wait_for(self.execute_guarded(row, fence), self.settings.worker_max_runtime_seconds)
        except Exception as exc:
            await self.record_failure(row, fence, exc)

    async def execute_claimed_job(self, job, fence):
        return await self.execute({**job, "lease_epoch": fence.epoch}, fence.owner)

