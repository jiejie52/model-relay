from __future__ import annotations
import asyncio
import json
import logging
import httpx
from app.execution_plane.plan_loader import ExecutionPlanLoader
from app.execution_plane.result_committer import ResultCommitter
from app.shared_contracts import ExecutionConflictError
from app.execution_plane.structured_output import StructuredOutputError
from app.protocol_adapters.base import ProviderRequestError
from app.core.error_classification import error_source
from app.core.idempotency import V3_FENCED_REQUEST_VERSIONS
from app.observability import error as log_error, exception_failure_class
from app.runtime.execution_guard import ExecutionGuard, ExecutionLeaseLost, execution_guard
from app.supabase import SupabaseError
from app.utils import compact_error_excerpt

logger = logging.getLogger("model-relay-runtime")


def _supabase_error_fields(exc: BaseException) -> dict[str, object]:
    """Extract structured Supabase/PostgREST error details without truncating JSON payloads."""
    target = exc if isinstance(exc, SupabaseError) else exc.__cause__
    if not isinstance(target, SupabaseError):
        return {}
    raw_body = (target.body or "").strip()
    if not raw_body:
        return {}
    try:
        parsed = json.loads(raw_body)
    except ValueError:
        return {"supabase_error_body": compact_error_excerpt(raw_body, 2048)}
    if isinstance(parsed, (dict, list)):
        return {"supabase_error": parsed}
    return {"supabase_error_body": compact_error_excerpt(raw_body, 2048)}


def is_transient_commit_failure(exc: BaseException) -> bool:
    """Return True only for transient commit failures eligible for commit-only retry."""
    if isinstance(exc, ExecutionLeaseLost):
        return True
    if isinstance(exc, (StructuredOutputError, ProviderRequestError, ExecutionConflictError)):
        return False
    if isinstance(exc, SupabaseError):
        return exc.status_code in {408, 429} or exc.status_code >= 500
    if isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.TransportError, ConnectionError)):
        return True
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return is_transient_commit_failure(cause)
    return False


def log_failure_observed(row, *, dispatch_state, exc, handling):
    """Emit structured error diagnostics when a request execution failure occurs."""
    log_error(
        logger,
        "request_execution_failure_observed",
        exc_info=exc,
        request_id=row.get("id"),
        session_id=row.get("session_id"),
        execution_mode=row.get("execution_mode"),
        provider_dispatch_state=dispatch_state,
        exception_type=type(exc).__name__,
        failure_class=exception_failure_class(exc),
        failure_code=getattr(exc, "code", None),
        http_status=getattr(exc, "status_code", None),
        handling=handling,
        error_message=str(exc) or None,
        **_supabase_error_fields(exc),
    )

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

    async def execute_guarded(self, row, fence):
        if fence is None:
            return await self.execute_row(row, fence)
        guard = ExecutionGuard(self.repo, row, fence, self.settings)

        async def run():
            with execution_guard(guard):
                await guard.verify()
                return await self.execute_row(row, fence)

        execution = asyncio.create_task(run())
        heartbeat = asyncio.create_task(guard.heartbeat())
        try:
            done, _ = await asyncio.wait((execution, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if execution in done:
                return await execution
            guard.invalidate(guard.reason)
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise ExecutionLeaseLost(guard.reason)
        finally:
            execution.cancel()
            heartbeat.cancel()
            await asyncio.gather(execution, heartbeat, return_exceptions=True)

    async def record_failure(self, row, fence, exc, *, force_terminal_sync: bool = False):
        if isinstance(exc, ExecutionLeaseLost) and not force_terminal_sync:
            log_failure_observed(
                row,
                dispatch_state=row.get("provider_dispatch_state"),
                exc=exc,
                handling="reconcile_lease_lost",
            )
            # Only authoritative reconciliation may resolve an expired lease;
            # this executor cannot use its stale fence to publish a terminal fact.
            try:
                await self.repo.reconcile_request(row["id"], tenant_id=row["tenant_id"],
                                                  conversation_hash=row["conversation_hash"])
            except Exception:
                pass  # durable state remains recoverable after connectivity returns
            return
        if isinstance(exc, ExecutionConflictError) and not force_terminal_sync:
            log_failure_observed(
                row,
                dispatch_state=row.get("provider_dispatch_state"),
                exc=exc,
                handling="ignored_execution_conflict",
            )
            # The execution right may have changed. Stale executors cannot
            # manufacture another right or overwrite another executor's facts.
            return
        current = await self.repo.get_request(row["id"]) or row
        state = current.get("provider_dispatch_state", "not_sent")
        contract_failure = isinstance(exc, (StructuredOutputError, ProviderRequestError))
        if (
            state == "result_stored"
            and not contract_failure
            and not force_terminal_sync
            and is_transient_commit_failure(exc)
        ):
            log_failure_observed(
                current,
                dispatch_state=state,
                exc=exc,
                handling="preserved_result_stored_for_commit_recovery",
            )
            # Raw facts are durable. Preserve commit-only recovery on transient
            # timeouts or 5xx/network failures during finalization.
            return
        is_v3 = current.get("request_identity_version") in V3_FENCED_REQUEST_VERSIONS
        if is_v3 and fence is None:
            log_failure_observed(
                current,
                dispatch_state=state,
                exc=exc,
                handling="missing_fence_for_terminal_failure",
            )
            return
        log_failure_observed(
            current,
            dispatch_state=state,
            exc=exc,
            handling="persist_terminal_failure",
        )
        error = await self.errors.record(exc=exc, source=error_source(exc), tenant_id=current["tenant_id"],
            conversation_hash=current["conversation_hash"], session_id=current["session_id"], request_id=current["id"])
        if state == "result_stored":
            if current.get("provisional_result_object_id") and not error.get("provider_success_object_id"):
                error["provider_success_object_id"] = current["provisional_result_object_id"]
            if current.get("provisional_output_object_id") and not error.get("provider_output_object_id"):
                error["provider_output_object_id"] = current["provisional_output_object_id"]
        unknown = state == "dispatch_started" and not contract_failure
        kwargs = dict(request_id=current["id"], session_id=current["session_id"], error=error,
                      status="indeterminate" if unknown else "failed", release_session=not unknown)
        if is_v3:
            await self.repo.fail_request_v3(**kwargs, fence_owner=fence.owner, fence_epoch=fence.epoch)
        else:
            await self.repo.fail_request(**kwargs, lease_owner=fence.owner if fence else None, lease_epoch=fence.epoch if fence else None)
