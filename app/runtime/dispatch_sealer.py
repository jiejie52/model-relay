from __future__ import annotations


import logging

from app.core.idempotency import stable_hash
from app.protocol_adapters.base import ProviderRequestError
from app.utils import utcnow


logger = logging.getLogger("model-relay-runtime")


from app.runtime.contracts import SealedDispatchBundle, dispatch_input_digest


class AtomicDispatchSealer:
    def __init__(self, repo): self.repo = repo

    async def seal_before_dispatch(self, state):
        if state.snapshot.get("placement_contract_version") is not None:
            from app.runtime.material_history import verify_pre_seal_material_invariants
            verify_pre_seal_material_invariants(state)
            from app.observability.material_metrics import material_metrics
            from app.observability import info as log_info
            log_info(logger, "material_placement_pre_seal", request_id=state.request_id, session_id=state.session_id,
                **material_metrics(state.snapshot, state.provider_physical_plan, state.historical_pinned_bindings, cache_binding=state.cache_binding))
        inputs = dict(snapshot=state.snapshot, session=state.session, history=state.history,
            material_ids=state.material_ids, material_bindings=state.material_bindings,
            tenant_id=state.request_row["tenant_id"], conversation_hash=state.request_row["conversation_hash"],
            request_id=state.request_id, session_id=state.session_id, context_plan=state.context_plan,
            cache_execution_binding=state.cache_binding, execution_fence=state.fence.canonical() if state.fence else None,
            provider_physical_plan=state.provider_physical_plan,
            affinity_headers={key: state.wire["affinity_token"] for key in (
                state.wire.get("affinity_header"), state.wire.get("secondary_affinity_header"))
                if key and state.wire.get("affinity_token")})
        expected_identity = (state.session.get("metadata") or {}).get("_relay_route", {}).get("connection_identity_hash")
        if expected_identity and state.snapshot.get("connection_identity_hash") != expected_identity:
            raise ProviderRequestError("CONNECTION_IDENTITY_MISMATCH", "Dispatch inputs changed the frozen connection")
        from app.runtime.contracts import frozen_connection_fields
        spec,scope=frozen_connection_fields(state.snapshot,(state.session.get('metadata') or {}).get('_relay_route'))
        if spec and (state.plan.connection_spec_hash!=spec or state.plan.account_scope_id!=scope):
            raise ProviderRequestError('CONNECTION_IDENTITY_MISMATCH','Dispatch connection differs from FrozenExecutionPlan')
        inputs.update(connection_spec_hash=spec,account_scope_id=scope)
        if state.snapshot.get("placement_contract_version") is not None:
            inputs.update(material_occurrences=state.projected_material_occurrences,
                historical_bindings=state.historical_pinned_bindings)
        prepared_payload_hash = dispatch_input_digest(**inputs)
        if state.is_v3:
            binding_hash = state.cache_binding_obj.binding_hash
            installed = await self.repo.install_cache_binding_v3(
                request_id=state.request_id,
                binding_version=state.cache_binding_obj.binding_version,
                plan_hash=state.cache_binding_obj.plan_hash,
                binding_hash=binding_hash,
                final_mechanism=state.cache_binding_obj.mechanism,
                resource_id=state.cache_binding_obj.resource_id,
                resource_generation=state.cache_binding_obj.resource_generation,
                metadata=state.cache_binding,
                fence_owner=state.fence.owner,
                fence_epoch=state.fence.epoch,
            )
            if not installed:
                raise ProviderRequestError(
                    "CACHE_BINDING_FENCE_REJECTED",
                    "Cache execution binding was rejected by the Request execution fence",
                )

            seal_method = self.repo.seal_materials_and_dispatch_v89 if state.snapshot.get("placement_contract_version") is not None else self.repo.seal_cache_and_dispatch_v3
            sealed = await seal_method(
                request_id=state.request_id,
                binding_version=state.cache_binding_obj.binding_version,
                binding_hash=binding_hash,
                payload_hash=prepared_payload_hash,
                **({"material_occurrences": state.projected_material_occurrences,
                    "historical_bindings": state.historical_pinned_bindings,
                    "safety_seconds": state.snapshot["historical_material_safety_seconds"]}
                   if state.snapshot.get("placement_contract_version") is not None else {}),
                fence_owner=state.fence.owner,
                fence_epoch=state.fence.epoch,
            )
            if not sealed:
                raise ProviderRequestError(
                    "CACHE_BINDING_FENCE_REJECTED",
                    "Request dispatch seal was rejected; Provider dispatch is forbidden",
                )
            state.execution_snapshot = dict(state.snapshot)
            state.execution_snapshot["_relay_cache_execution"] = state.cache_binding
        else:
            if state.fence is not None:
                from app.runtime.execution_guard import ExecutionLeaseLost
                if not await self.repo.mark_request_dispatch(request_id=state.request_id,
                        fence_owner=state.fence.owner, fence_epoch=state.fence.epoch,
                        payload_hash=prepared_payload_hash):
                    raise ExecutionLeaseLost("Legacy fenced dispatch was rejected")
            else:
                # Preserve the historical optional-fence synchronous contract.
                await self.repo.update_request(state.request_row["id"], {
                    "provider_dispatch_state": "dispatch_started",
                    "started_at": state.request_row.get("started_at") or utcnow().isoformat()})


        state.sealed_bundle = SealedDispatchBundle(**{**inputs, "snapshot": state.execution_snapshot or state.snapshot},
            seal_id=state.request_id, seal_hash=prepared_payload_hash)
        state.sealed_bundle.verify_integrity()
        return state.sealed_bundle
