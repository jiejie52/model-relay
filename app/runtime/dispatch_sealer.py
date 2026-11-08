from __future__ import annotations


import logging

from app.core.idempotency import stable_hash
from app.protocol_adapters.base import ProviderRequestError
from app.utils import utcnow


logger = logging.getLogger("model-relay-runtime")


from app.runtime.contracts import SealedDispatchBundle


class AtomicDispatchSealer:
    def __init__(self, repo): self.repo = repo

    async def seal_before_dispatch(self, state):
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

            # This digest covers the exact frozen inputs supplied to the Adapter.
            # Provider-specific byte-level payload builders can strengthen this
            # to a native-wire digest without changing the dispatch transaction.
            prepared_payload_hash = stable_hash(
                {
                    "schema_version": "relay-prepared-execution/1",
                    "snapshot": state.snapshot,
                    "history": state.history,
                    "material_bindings": state.material_bindings,
                    "provider_physical_plan_hash": (state.provider_physical_plan or {}).get("physical_plan_hash"),
                    "cache_binding": state.cache_binding,
                }
            )
            sealed = await self.repo.seal_cache_and_dispatch_v3(
                request_id=state.request_id,
                binding_version=state.cache_binding_obj.binding_version,
                binding_hash=binding_hash,
                payload_hash=prepared_payload_hash,
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
            await self.repo.update_request(
                state.request_row["id"],
                {
                    "provider_dispatch_state": "dispatch_started",
                    "started_at": state.request_row.get("started_at") or utcnow().isoformat(),
                },
            )


        state.sealed_bundle = SealedDispatchBundle(
            snapshot=state.execution_snapshot or state.snapshot, session=state.session, history=state.history,
            material_ids=state.material_ids, material_bindings=state.material_bindings,
            tenant_id=state.request_row["tenant_id"], conversation_hash=state.request_row["conversation_hash"],
            request_id=state.request_id, session_id=state.session_id, context_plan=state.context_plan,
            cache_execution_binding=state.cache_binding, execution_fence=state.fence.canonical() if state.fence else None,
            provider_physical_plan=state.provider_physical_plan, seal_id=state.request_id,
            seal_hash=prepared_payload_hash if state.is_v3 else stable_hash(state.execution_snapshot),
            affinity_headers={key: state.wire["affinity_token"] for key in (
                state.wire.get("affinity_header"), state.wire.get("secondary_affinity_header"))
                if key and state.wire.get("affinity_token")},
        )
        return state.sealed_bundle
