from __future__ import annotations
from app.runtime.contracts import RuntimeState, ActualExecutionFact
from app.runtime.condition_verifier import ConditionVerifier
from app.runtime.physical_context import PhysicalContextProjector
from app.runtime.dispatch_sealer import AtomicDispatchSealer
from app.runtime.controlled_invoker import ControlledInvoker
from app.runtime.cache_materializer import CacheMaterializer
from app.core.raw_error import RawErrorRecorder
from app.persistence.execution_artifacts import ExecutionArtifacts
from app.observability import now_ms
from app.runtime.execution_guard import ExecutionGuard, ExecutionLeaseLost, ensure_execution_allowed, current_execution_guard, execution_guard

class RelayRuntimeEngine:
    """Physical execution only; schedules and Session commits belong to the execution plane."""
    def __init__(self, repo, storage, providers, materials, bindings, settings, cache_orchestrator=None):
        self.repo, self.storage, self.settings = repo, storage, settings
        self.providers = providers
        self.artifacts = ExecutionArtifacts(repo, storage, settings)
        self.errors = RawErrorRecorder(repo, storage, settings)
        self.verifier = ConditionVerifier(repo, providers)
        self.bindings = bindings
        self.projector = PhysicalContextProjector(repo, bindings, settings)
        self.cache = cache_orchestrator or CacheMaterializer(repo)
        self.cache.settings = settings
        self.sealer = AtomicDispatchSealer(repo)
        self.invoker = ControlledInvoker(repo, providers, self.artifacts, self.cache, settings)

    async def execute(self, plan):
        from app.runtime.connection_scope import connection_scope
        identity = plan.snapshot.get("connection_identity_hash")
        route = (plan.session_record.get("metadata") or {}).get("_relay_route") or {}
        if not identity and route.get("account_scope_hash") and hasattr(self.providers, "frozen_identity"):
            identity = self.providers.frozen_identity(plan.snapshot["connection_id"], route)
        with connection_scope(identity):
            return await self._execute_with_connection(plan)

    async def _execute_with_connection(self, plan):
        if plan.execution_fence is not None and current_execution_guard() is None:
            # Direct physical-runtime callers still need the live boundary checks;
            # scheduling and heartbeat ownership stay in the execution plane.
            with execution_guard(ExecutionGuard(self.repo, plan.request_record, plan.execution_fence, self.settings)):
                return await self._execute(plan)
        return await self._execute(plan)

    async def _execute(self, plan):
        state = RuntimeState.from_plan(plan, now_ms())
        try:
            await self.verifier.verify(plan)
            await ensure_execution_allowed(states=("not_sent",))
            await self.projector.prepare_scope(state)
            state.failure_stage = "material_bind"
            await self.bindings.bind_for_plan(state)
            await ensure_execution_allowed(states=("not_sent",))
            state.failure_stage = "cache_assessment"
            await self.projector.project(state)
            state.execution_snapshot = state.snapshot
            state.failure_stage = "cache_materialize"
            if state.is_v3: await self.cache.prepare_for_context(state)
            await ensure_execution_allowed(states=("not_sent",))
            state.failure_stage = "dispatch_seal"
            await self.sealer.seal_before_dispatch(state)
            state.failure_stage = "provider_http"
            return await self.invoker.invoke_once_and_record(state)
        except ExecutionLeaseLost:
            raise
        except Exception as exc:
            # asyncio cancellation is deliberately propagated to the scheduler.
            current = await self.repo.get_request(state.request_id) or state.request_row
            from app.core.error_classification import error_source
            error = await self.errors.record(exc=exc, source=error_source(exc), tenant_id=state.request_row["tenant_id"],
                conversation_hash=state.request_row["conversation_hash"], session_id=state.session_id, request_id=state.request_id)
            error = {**error, "exception_type": type(exc).__name__, "failure_code": getattr(exc, "code", None),
                     "unknown_completion": current.get("provider_dispatch_state")=="dispatch_started" and
                         (bool(getattr(exc, "stream_interrupted", False)) or
                          not (getattr(exc, "status_code", None) is not None or type(exc).__name__=="ProviderRequestError"))}
            return ActualExecutionFact(state.request_id, current.get("provider_dispatch_state", "not_sent"),
                state.material_bindings, state.cache_binding, raw_error_envelope=error, failure_stage=state.failure_stage)
