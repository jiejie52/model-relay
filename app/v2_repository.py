from __future__ import annotations

from typing import Any
from uuid import UUID

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

