from __future__ import annotations
import asyncio
from uuid import uuid4
from app.shared_contracts import ExecutionFence
from app.execution_plane.schedulers.common import RequestExecutionService

class InlineScheduler(RequestExecutionService):
    async def execute(self, request_row):
        is_v3 = request_row.get("request_identity_version") in {"relay-request/2.3", "relay-request/2.4"}
        fence = None
        if is_v3:
            owner = f"sync:{uuid4()}"
            acquire = (self.repo.acquire_sync_commit_fence if request_row.get("provider_dispatch_state") == "result_stored"
                       else self.repo.acquire_sync_request_fence)
            epoch = await acquire(str(request_row["id"]), owner,
                lease_seconds=int(self.settings.sync_request_deadline_seconds)+30)
            if epoch is None: return await self.repo.get_request(request_row["id"])
            fence = ExecutionFence(owner, epoch, kind="sync_request")
        try:
            await asyncio.wait_for(self.execute_guarded(request_row, fence), self.settings.sync_request_deadline_seconds)
        except Exception as exc:
            await self.record_failure(request_row, fence, exc)
        finally:
            if fence is not None:
                try:
                    # The child execution has stopped. Release only a durable
                    # result's commit right, never an in-flight inference right.
                    await self.repo.release_sync_commit_fence(str(request_row["id"]), fence.owner, fence.epoch)
                except Exception:
                    pass  # expiry still permits later commit-only recovery
        return await self.repo.get_request(request_row["id"])

    execute_sync = execute

