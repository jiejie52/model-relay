from __future__ import annotations

from typing import Any
from uuid import UUID
from datetime import datetime, timezone
import hashlib
import json

from .repository import RelayRepository


class RelayV2Repository(RelayRepository):
    async def find_session_by_idempotency(
        self, tenant_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "relay_sessions",
            filters={
                "tenant_id": f"eq.{tenant_id}",
                "idempotency_key": f"eq.{idempotency_key}",
            },
            limit=1,
        )
        return rows[0] if rows else None

    async def find_request_by_idempotency(
        self, session_id: str | UUID, idempotency_key: str
    ) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "relay_requests",
            filters={
                "session_id": f"eq.{session_id}",
                "idempotency_key": f"eq.{idempotency_key}",
            },
            limit=1,
        )
        return rows[0] if rows else None

    async def get_request(
        self,
        request_id: str | UUID,
        *,
        tenant_id: str | None = None,
        conversation_hash: str | None = None,
        session_id: str | UUID | None = None,
    ) -> dict[str, Any] | None:
        filters = {"id": f"eq.{request_id}"}
        if tenant_id:
            filters["tenant_id"] = f"eq.{tenant_id}"
        if conversation_hash:
            filters["conversation_hash"] = f"eq.{conversation_hash}"
        if session_id:
            filters["session_id"] = f"eq.{session_id}"
        rows = await self.backend.select("relay_requests", filters=filters, limit=1)
        return rows[0] if rows else None

    async def update_request(
        self,
        request_id: str | UUID,
        values: dict[str, Any],
    ) -> dict[str, Any] | None:
        rows = await self.backend.update(
            "relay_requests", values, filters={"id": f"eq.{request_id}"}
        )
        return rows[0] if rows else None

    async def accept_request(
        self,
        *,
        session_id: str,
        request_id: str,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
        request_hash: str,
        execution_mode: str,
        request_object_id: str,
        provider: str,
        connection_id: str,
        model: str,
        execution_pool: str,
        metadata: dict[str, Any],
        job_id: str | None,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "accept_relay_request",
            {
                "p_session_id": session_id,
                "p_request_id": request_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_idempotency_key": idempotency_key,
                "p_request_hash": request_hash,
                "p_execution_mode": execution_mode,
                "p_request_object_id": request_object_id,
                "p_provider": provider,
                "p_connection_id": connection_id,
                "p_model": model,
                "p_execution_pool": execution_pool,
                "p_metadata": metadata,
                "p_job_id": job_id,
                "p_job_expires_at": self.default_job_expiry().isoformat(),
            },
        )
        if isinstance(result, list):
            if not result:
                raise RuntimeError("accept_relay_request returned no row")
            return result[0]
        if isinstance(result, dict):
            return result
        raise RuntimeError("accept_relay_request returned invalid data")


    async def accept_request_v3(
        self,
        *,
        session_id: str,
        request_id: str,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
        request_hash: str,
        caller_intent_hash: str,
        request_identity_version: str,
        execution_mode: str,
        request_object_id: str,
        context_plan_object_id: str,
        context_plan_hash: str,
        cache_plan_object_id: str,
        cache_plan_hash: str,
        cache_resolution_status: str,
        provider: str,
        connection_id: str,
        model: str,
        execution_pool: str,
        expected_history_version: int,
        metadata: dict[str, Any],
        job_id: str | None,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "accept_relay_request_v3",
            {
                "p_session_id": session_id,
                "p_request_id": request_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_idempotency_key": idempotency_key,
                "p_request_hash": request_hash,
                "p_caller_intent_hash": caller_intent_hash,
                "p_request_identity_version": request_identity_version,
                "p_execution_mode": execution_mode,
                "p_request_object_id": request_object_id,
                "p_context_plan_object_id": context_plan_object_id,
                "p_context_plan_hash": context_plan_hash,
                "p_cache_plan_object_id": cache_plan_object_id,
                "p_cache_plan_hash": cache_plan_hash,
                "p_cache_resolution_status": cache_resolution_status,
                "p_provider": provider,
                "p_connection_id": connection_id,
                "p_model": model,
                "p_execution_pool": execution_pool,
                "p_expected_history_version": expected_history_version,
                "p_metadata": metadata,
                "p_job_id": job_id,
                "p_job_expires_at": self.default_job_expiry().isoformat(),
            },
        )
        if isinstance(result, list):
            if not result:
                raise RuntimeError("accept_relay_request_v3 returned no row")
            return result[0]
        if isinstance(result, dict):
            return result
        raise RuntimeError("accept_relay_request_v3 returned invalid data")

    async def acquire_sync_request_fence(
        self, request_id: str, executor_id: str, *, lease_seconds: int | None = None
    ) -> int | None:
        result = await self.backend.rpc(
            "acquire_sync_request_fence",
            {
                "p_request_id": request_id,
                "p_executor_id": executor_id,
                "p_lease_seconds": int(lease_seconds or self.settings.sync_request_deadline_seconds),
            },
        )
        try:
            value = int(result)
        except Exception:
            return None
        return value if value > 0 else None

    async def renew_sync_request_fence(
        self, request_id: str, executor_id: str, executor_epoch: int, *, lease_seconds: int | None = None
    ) -> bool:
        result = await self.backend.rpc(
            "renew_sync_request_fence",
            {
                "p_request_id": request_id,
                "p_executor_id": executor_id,
                "p_executor_epoch": executor_epoch,
                "p_lease_seconds": int(lease_seconds or self.settings.sync_request_deadline_seconds),
            },
        )
        return bool(result)

    async def install_request_material_binding_v3(
        self, *, request_id: str, snapshot: list[dict[str, Any]], fence_owner: str, fence_epoch: int
    ) -> bool:
        return bool(
            await self.backend.rpc(
                "install_request_material_binding_v3",
                {
                    "p_request_id": request_id,
                    "p_snapshot": snapshot,
                    "p_fence_owner": fence_owner,
                    "p_fence_epoch": fence_epoch,
                },
            )
        )

    async def install_cache_binding_v3(
        self,
        *,
        request_id: str,
        binding_version: int,
        plan_hash: str,
        binding_hash: str,
        final_mechanism: str | None,
        resource_id: str | None,
        resource_generation: int | None,
        metadata: dict[str, Any],
        fence_owner: str,
        fence_epoch: int,
    ) -> bool:
        return bool(
            await self.backend.rpc(
                "install_cache_binding_v3",
                {
                    "p_request_id": request_id,
                    "p_binding_version": binding_version,
                    "p_plan_hash": plan_hash,
                    "p_binding_hash": binding_hash,
                    "p_final_mechanism": final_mechanism,
                    "p_resource_id": resource_id,
                    "p_resource_generation": resource_generation,
                    "p_metadata": metadata,
                    "p_fence_owner": fence_owner,
                    "p_fence_epoch": fence_epoch,
                },
            )
        )

    async def seal_cache_and_dispatch_v3(
        self,
        *,
        request_id: str,
        binding_version: int,
        binding_hash: str,
        payload_hash: str,
        fence_owner: str,
        fence_epoch: int,
    ) -> bool:
        return bool(
            await self.backend.rpc(
                "seal_cache_and_dispatch_v3",
                {
                    "p_request_id": request_id,
                    "p_binding_version": binding_version,
                    "p_binding_hash": binding_hash,
                    "p_payload_hash": payload_hash,
                    "p_fence_owner": fence_owner,
                    "p_fence_epoch": fence_epoch,
                },
            )
        )

    async def release_cache_pins_v3(self, request_id: str, *, hold_until: str | None = None) -> int:
        result = await self.backend.rpc(
            "release_cache_pins_v3",
            {"p_request_id": request_id, "p_hold_until": hold_until},
        )
        try:
            return int(result or 0)
        except Exception:
            return 0

    @staticmethod
    def cache_scope_hash(
        *,
        tenant_id: str,
        conversation_hash: str,
        session_id: str,
        offering_id: str,
        account_scope_hash: str,
        protocol_profile_hash: str,
    ) -> str:
        raw = json.dumps(
            {
                "tenant_id": tenant_id,
                "conversation_hash": conversation_hash,
                "session_id": session_id,
                "offering_id": offering_id,
                "account_scope_hash": account_scope_hash,
                "protocol_profile_hash": protocol_profile_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    async def find_ready_cache_resource(
        self, *, scope_hash: str, content_fingerprint: str
    ) -> dict[str, Any] | None:
        # Fetch a small generation window and apply the expiry predicate here so
        # both expiring and non-expiring resources can be handled without relying
        # on a PostgREST OR expression. Expired rows remain audit facts but are
        # never reused for a new Request binding.
        rows = await self.backend.select(
            "relay_cache_resources",
            filters={
                "scope_hash": f"eq.{scope_hash}",
                "content_fingerprint": f"eq.{content_fingerprint}",
                "state": "eq.ready",
            },
            order="generation.desc",
            limit=10,
        )
        now = datetime.now(timezone.utc)
        for row in rows:
            expire_time = row.get("expire_time")
            if not expire_time:
                if row.get("provider_handle_ref"):
                    return row
                continue
            try:
                parsed = datetime.fromisoformat(str(expire_time).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except Exception:
                # An unparseable expiry is not evidence that the resource is safe
                # to reuse. Keep searching older generations.
                continue
            if parsed > now and row.get("provider_handle_ref"):
                return row
        return None

    async def find_compatible_cache_resources(
        self,
        *,
        scope_hash: str,
        reuse_key: str,
        max_prefix_version: int,
    ) -> list[dict[str, Any]]:
        rows = await self.backend.select(
            "relay_cache_resources",
            filters={
                "scope_hash": f"eq.{scope_hash}",
                "reuse_key": f"eq.{reuse_key}",
                "state": "eq.ready",
                "prefix_version": f"lte.{max(0, int(max_prefix_version))}",
            },
            order="prefix_version.desc,generation.desc",
            limit=20,
        )
        now = datetime.now(timezone.utc)
        safety = max(0, int(getattr(self.settings, "cache_expiry_safety_seconds", 30)))
        usable: list[dict[str, Any]] = []
        for row in rows:
            if not row.get("provider_handle_ref"):
                continue
            expire_time = row.get("expire_time")
            if expire_time:
                try:
                    parsed = datetime.fromisoformat(str(expire_time).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if (parsed - now).total_seconds() <= safety:
                    continue
            usable.append(row)
        return usable

    async def start_cache_operation(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        lease_epoch: int,
    ) -> bool:
        result = await self.backend.rpc(
            "start_cache_operation_v31",
            {
                "p_operation_id": operation_id,
                "p_lease_owner": lease_owner,
                "p_lease_epoch": int(lease_epoch),
            },
        )
        return bool(result)

    async def record_cache_operation_observation(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        lease_epoch: int,
        raw_result: dict[str, Any],
        provider_request_id: str | None = None,
    ) -> bool:
        result = await self.backend.rpc(
            "record_cache_operation_observation_v31",
            {
                "p_operation_id": operation_id,
                "p_lease_owner": lease_owner,
                "p_lease_epoch": int(lease_epoch),
                "p_raw_result": raw_result,
                "p_provider_request_id": provider_request_id,
            },
        )
        return bool(result)

    async def finish_cache_operation(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        lease_epoch: int,
        state: str,
        raw_result: dict[str, Any],
    ) -> bool:
        result = await self.backend.rpc(
            "finish_cache_operation_v31",
            {
                "p_operation_id": operation_id,
                "p_lease_owner": lease_owner,
                "p_lease_epoch": int(lease_epoch),
                "p_state": state,
                "p_raw_result": raw_result,
            },
        )
        return bool(result)

    async def invalidate_cache_resource(
        self,
        *,
        resource_id: str,
        provider_handle_ref: str,
        state: str,
    ) -> bool:
        result = await self.backend.rpc(
            "invalidate_cache_resource_v31",
            {
                "p_resource_id": resource_id,
                "p_provider_handle_ref": provider_handle_ref,
                "p_state": state,
            },
        )
        return bool(result)

    async def create_cache_operation_intent(
        self,
        *,
        request_id: str,
        scope_hash: str,
        content_fingerprint: str,
        operation_type: str,
        lease_owner: str,
        lease_epoch: int,
    ) -> dict[str, Any]:
        # Resource creation is singleflight by scope + content + operation, not
        # by Request. Different Requests targeting the same frozen CacheSpec must
        # contend for one Provider-side create operation instead of each creating
        # a duplicate resource.
        idempotency_key = (
            f"cache:{operation_type}:{scope_hash[:24]}:{content_fingerprint[:24]}"
        )
        result = await self.backend.rpc(
            "claim_cache_operation_v3",
            {
                "p_request_id": request_id,
                "p_scope_hash": scope_hash,
                "p_content_fingerprint": content_fingerprint,
                "p_operation_type": operation_type,
                "p_idempotency_key": idempotency_key,
                "p_lease_owner": lease_owner,
                "p_lease_seconds": max(60, int(getattr(self.settings, "cache_prepare_timeout_seconds", 60)) + 30),
            },
        )
        row = result[0] if isinstance(result, list) and result else result if isinstance(result, dict) else None
        if not isinstance(row, dict):
            raise RuntimeError("Cache operation could not be claimed")
        # The database owns the cache operation epoch. The Request execution
        # epoch is intentionally separate.
        return row

    async def publish_cache_resource(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        lease_epoch: int,
        tenant_id: str,
        conversation_hash: str,
        session_id: str,
        offering_id: str,
        connection_id: str,
        scope_hash: str,
        content_fingerprint: str,
        reuse_key: str,
        prefix_version: int,
        token_count: int | None,
        spec_hash: str,
        provider_handle_ref: str,
        expire_time: str | None,
        profile_hash: str,
        raw_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        op_epoch = int(raw_result.get("operation_epoch") or lease_epoch)
        result = await self.backend.rpc(
            "publish_cache_resource_v31",
            {
                "p_operation_id": operation_id,
                "p_lease_owner": lease_owner,
                "p_lease_epoch": op_epoch,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_session_id": session_id,
                "p_offering_id": offering_id,
                "p_connection_id": connection_id,
                "p_scope_hash": scope_hash,
                "p_profile_hash": profile_hash,
                "p_content_fingerprint": content_fingerprint,
                "p_reuse_key": reuse_key,
                "p_prefix_version": int(prefix_version),
                "p_token_count": token_count,
                "p_spec_hash": spec_hash,
                "p_provider_handle_ref": provider_handle_ref,
                "p_expire_time": expire_time,
                "p_raw_result": raw_result,
            },
        )
        if isinstance(result, list):
            return result[0] if result else None
        return result if isinstance(result, dict) else None

    async def store_result_v3(
        self,
        *,
        request_id: str,
        result_object_id: str,
        output_object_id: str | None,
        history_object_id: str | None,
        compact_result: dict[str, Any],
        provider_response_id: str | None,
        cache_usage: dict[str, Any] | None,
        fence_owner: str,
        fence_epoch: int,
    ) -> bool:
        result = await self.backend.rpc(
            "store_relay_result_v3",
            {
                "p_request_id": request_id,
                "p_result_object_id": result_object_id,
                "p_output_object_id": output_object_id,
                "p_history_object_id": history_object_id,
                "p_compact_result": compact_result,
                "p_provider_response_id": provider_response_id,
                "p_cache_usage": cache_usage,
                "p_fence_owner": fence_owner,
                "p_fence_epoch": fence_epoch,
            },
        )
        return bool(result)

    async def complete_request_v3(
        self,
        *,
        request_id: str,
        session_id: str,
        history_object_id: str | None,
        result_object_id: str,
        output_object_id: str | None,
        compact_result: dict[str, Any],
        provider_response_id: str | None,
        expected_history_version: int,
        fence_owner: str,
        fence_epoch: int,
    ) -> bool:
        result = await self.backend.rpc(
            "complete_relay_request_v3",
            {
                "p_request_id": request_id,
                "p_session_id": session_id,
                "p_expected_history_version": expected_history_version,
                "p_history_object_id": history_object_id,
                "p_result_object_id": result_object_id,
                "p_output_object_id": output_object_id,
                "p_compact_result": compact_result,
                "p_provider_response_id": provider_response_id,
                "p_fence_owner": fence_owner,
                "p_fence_epoch": fence_epoch,
            },
        )
        return bool(result)

    async def fail_request_v3(
        self,
        *,
        request_id: str,
        session_id: str,
        error: dict[str, Any],
        status: str,
        release_session: bool,
        fence_owner: str,
        fence_epoch: int,
        hold_pins_until: str | None = None,
    ) -> bool:
        result = await self.backend.rpc(
            "fail_relay_request_v3",
            {
                "p_request_id": request_id,
                "p_session_id": session_id,
                "p_status": status,
                "p_error": error,
                "p_release_session": release_session,
                "p_fence_owner": fence_owner,
                "p_fence_epoch": fence_epoch,
                "p_hold_pins_until": hold_pins_until,
            },
        )
        return bool(result)

    async def complete_request(
        self,
        *,
        request_id: str,
        session_id: str,
        history_object_id: str | None,
        result_object_id: str,
        output_object_id: str | None,
        compact_result: dict[str, Any],
        provider_response_id: str | None,
        expected_history_version: int,
        lease_owner: str | None = None,
        lease_epoch: int | None = None,
    ) -> bool:
        result = await self.backend.rpc(
            "complete_relay_request",
            {
                "p_request_id": request_id,
                "p_session_id": session_id,
                "p_expected_history_version": expected_history_version,
                "p_history_object_id": history_object_id,
                "p_result_object_id": result_object_id,
                "p_output_object_id": output_object_id,
                "p_compact_result": compact_result,
                "p_provider_response_id": provider_response_id,
                "p_lease_owner": lease_owner,
                "p_lease_epoch": lease_epoch,
            },
        )
        return bool(result)

    async def fail_request(
        self,
        *,
        request_id: str,
        session_id: str,
        error: dict[str, Any],
        status: str = "failed",
        release_session: bool = True,
        lease_owner: str | None = None,
        lease_epoch: int | None = None,
    ) -> bool:
        result = await self.backend.rpc(
            "fail_relay_request",
            {
                "p_request_id": request_id,
                "p_session_id": session_id,
                "p_status": status,
                "p_error": error,
                "p_release_session": release_session,
                "p_lease_owner": lease_owner,
                "p_lease_epoch": lease_epoch,
            },
        )
        return bool(result)

    async def reconcile_request(
        self, request_id: str | UUID, *, tenant_id: str, conversation_hash: str
    ) -> bool:
        result = await self.backend.rpc(
            "reconcile_relay_request",
            {
                "p_request_id": str(request_id),
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_sync_timeout_seconds": self.settings.sync_request_deadline_seconds,
            },
        )
        return bool(result)

    async def cancel_request(self, request_id: str, session_id: str) -> bool:
        result = await self.backend.rpc(
            "cancel_relay_request",
            {"p_request_id": request_id, "p_session_id": session_id},
        )
        return bool(result)

    async def claim_job_v2(self, worker_id: str) -> dict[str, Any] | None:
        result = await self.backend.rpc(
            "claim_relay_job_v2",
            {
                "p_worker_id": worker_id,
                "p_execution_pools": sorted(self.settings.worker_pool_set),
                "p_lease_seconds": self.settings.job_lease_seconds,
            },
        )
        if isinstance(result, list):
            return result[0] if result else None
        return result if isinstance(result, dict) else None

    async def renew_lease_v2(
        self, job_id: str, worker_id: str, lease_epoch: int
    ) -> bool:
        result = await self.backend.rpc(
            "renew_relay_job_lease_v2",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_lease_epoch": lease_epoch,
                "p_lease_seconds": self.settings.job_lease_seconds,
            },
        )
        return bool(result)

    async def create_object(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.insert("relay_objects", row)
        return rows[0]

    async def get_object(
        self,
        object_id: str,
        *,
        tenant_id: str | None = None,
        conversation_hash: str | None = None,
    ) -> dict[str, Any] | None:
        filters = {"id": f"eq.{object_id}"}
        if tenant_id:
            filters["tenant_id"] = f"eq.{tenant_id}"
        if conversation_hash:
            filters["conversation_hash"] = f"eq.{conversation_hash}"
        rows = await self.backend.select("relay_objects", filters=filters, limit=1)
        return rows[0] if rows else None

    async def find_material_by_idempotency(
        self, tenant_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "relay_materials",
            filters={
                "tenant_id": f"eq.{tenant_id}",
                "idempotency_key": f"eq.{idempotency_key}",
            },
            limit=1,
        )
        return rows[0] if rows else None

    async def create_material(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.insert("relay_materials", row)
        return rows[0]

    async def update_material(self, material_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        rows = await self.backend.update(
            "relay_materials", values, filters={"id": f"eq.{material_id}"}
        )
        return rows[0] if rows else None

    async def get_material(
        self,
        material_id: str,
        *,
        tenant_id: str | None = None,
        conversation_hash: str | None = None,
    ) -> dict[str, Any] | None:
        filters = {"id": f"eq.{material_id}"}
        if tenant_id:
            filters["tenant_id"] = f"eq.{tenant_id}"
        if conversation_hash:
            filters["conversation_hash"] = f"eq.{conversation_hash}"
        rows = await self.backend.select("relay_materials", filters=filters, limit=1)
        return rows[0] if rows else None

    async def get_materials(
        self,
        material_ids: list[str],
        *,
        tenant_id: str,
        conversation_hash: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for material_id in material_ids:
            row = await self.get_material(
                material_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
            )
            if row:
                result.append(row)
        return result

    async def get_provider_binding(
        self,
        *,
        material_id: str,
        connection_id: str,
        purpose: str | None = None,
        representation: str | None = None,
        adapter_version: str | None = None,
        account_scope_hash: str | None = None,
    ) -> dict[str, Any] | None:
        filters: dict[str, str] = {
            "material_id": f"eq.{material_id}",
            "connection_id": f"eq.{connection_id}",
        }
        if purpose is not None:
            filters["purpose"] = f"eq.{purpose}"
        if representation is not None:
            filters["representation"] = f"eq.{representation}"
        if adapter_version is not None:
            filters["adapter_version"] = f"eq.{adapter_version}"
        if account_scope_hash is not None:
            filters["account_scope_hash"] = f"eq.{account_scope_hash}"
        rows = await self.backend.select(
            "provider_material_bindings", filters=filters, limit=1, order="generation.desc"
        )
        return rows[0] if rows else None

    async def upsert_provider_binding(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.upsert(
            "provider_material_bindings",
            row,
            on_conflict=(
                "material_id,connection_id,account_scope_hash,purpose,representation,"
                "adapter_version,generation"
            ),
        )
        return rows[0]

    async def get_material_fallback(self, material_id: str) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "material_fallback_objects",
            filters={"material_id": f"eq.{material_id}"},
            limit=1,
        )
        return rows[0] if rows else None

    async def upsert_material_fallback(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.upsert(
            "material_fallback_objects", row, on_conflict="material_id"
        )
        return rows[0]

    async def delete_material_fallback(self, material_id: str) -> None:
        await self.backend.delete(
            "material_fallback_objects", filters={"material_id": f"eq.{material_id}"}
        )

    async def delete_object(self, object_id: str) -> None:
        await self.backend.delete("relay_objects", filters={"id": f"eq.{object_id}"})

    async def create_binding_attempt(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.insert("material_binding_attempts", row)
        return rows[0]

    async def update_binding_attempt(
        self, attempt_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        rows = await self.backend.update(
            "material_binding_attempts", values, filters={"attempt_id": f"eq.{attempt_id}"}
        )
        return rows[0] if rows else None

