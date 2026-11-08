from __future__ import annotations
import asyncio
from app.execution_plane.plan_loader import ExecutionPlanLoader
from app.execution_plane.result_committer import ResultCommitter
from app.shared_contracts import ExecutionConflictError
from app.execution_plane.structured_output import StructuredOutputError
from app.protocol_adapters.base import ProviderRequestError
from app.core.error_classification import error_source

class RequestExecutionService:
    def __init__(self, runtime, errors, repo, settings):
        self.runtime, self.errors, self.repo, self.settings = runtime, errors, repo, settings
        self.loader = ExecutionPlanLoader(repo, runtime.storage, settings)
        self.committer = ResultCommitter(repo, runtime.storage, settings, errors)

    async def execute_row(self, row, fence):
        plan = await self.loader.load(row, fence)
        state = row.get("provider_dispatch_state", "not_sent")
        if state == "result_stored":
            return await self.committer.recover_and_commit(plan)
        if state != "not_sent":
            raise ExecutionConflictError("An already dispatched Request cannot be sent again")
        fact = await self.runtime.execute(plan)
        return await self.committer.finalize(plan, fact)

    async def record_failure(self, row, fence, exc):
        if isinstance(exc, ExecutionConflictError):
            # The execution right may have changed. Stale executors cannot
            # manufacture another right or overwrite another executor's facts.
            return
        current = await self.repo.get_request(row["id"]) or row
        state = current.get("provider_dispatch_state", "not_sent")
        contract_failure = isinstance(exc, (StructuredOutputError, ProviderRequestError))
        if state == "result_stored" and not contract_failure:
            # Raw facts are durable. Preserve commit-only recovery on timeouts
            # or transient storage/database failures during finalization.
            return
        error = await self.errors.record(exc=exc, source=error_source(exc), tenant_id=row["tenant_id"],
            conversation_hash=row["conversation_hash"], session_id=row["session_id"], request_id=row["id"])
        unknown = state == "dispatch_started" and not contract_failure
        kwargs = dict(request_id=row["id"], session_id=row["session_id"], error=error,
                      status="indeterminate" if unknown else "failed", release_session=not unknown)
        if row.get("request_identity_version") == "relay-request/2.3":
            await self.repo.fail_request_v3(**kwargs, fence_owner=fence.owner, fence_epoch=fence.epoch)
        else:
            await self.repo.fail_request(**kwargs, lease_owner=fence.owner if fence else None, lease_epoch=fence.epoch if fence else None)
