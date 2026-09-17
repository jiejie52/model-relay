from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from .config import Settings
from .supabase import SupabaseBackend
from .utils import utcnow


class RelayRepository:
    """Persistence operations owned by Relay Core only."""

    def __init__(self, backend: SupabaseBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings

    async def find_job_by_idempotency(
        self,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "relay_jobs",
            filters={
                "tenant_id": f"eq.{tenant_id}",
                "conversation_hash": f"eq.{conversation_hash}",
                "idempotency_key": f"eq.{idempotency_key}",
            },
            limit=1,
        )
        return rows[0] if rows else None

    async def find_session_by_idempotency(
        self,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "relay_sessions",
            filters={
                "tenant_id": f"eq.{tenant_id}",
                "conversation_hash": f"eq.{conversation_hash}",
                "idempotency_key": f"eq.{idempotency_key}",
            },
            limit=1,
        )
        return rows[0] if rows else None

    async def create_session(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.insert("relay_sessions", row)
        return rows[0]

    async def get_session(
        self,
        session_id: str | UUID,
        *,
        tenant_id: str | None = None,
        conversation_hash: str | None = None,
    ) -> dict[str, Any] | None:
        filters = {"id": f"eq.{session_id}"}
        if tenant_id:
            filters["tenant_id"] = f"eq.{tenant_id}"
        if conversation_hash:
            filters["conversation_hash"] = f"eq.{conversation_hash}"
        rows = await self.backend.select("relay_sessions", filters=filters, limit=1)
        return rows[0] if rows else None

    async def update_session(
        self, session_id: str | UUID, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        rows = await self.backend.update(
            "relay_sessions", values, filters={"id": f"eq.{session_id}"}
        )
        return rows[0] if rows else None

    async def create_job(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.insert("relay_jobs", row)
        return rows[0]

    async def get_job(
        self,
        job_id: str | UUID,
        *,
        tenant_id: str | None = None,
        conversation_hash: str | None = None,
    ) -> dict[str, Any] | None:
        filters = {"id": f"eq.{job_id}"}
        if tenant_id:
            filters["tenant_id"] = f"eq.{tenant_id}"
        if conversation_hash:
            filters["conversation_hash"] = f"eq.{conversation_hash}"
        rows = await self.backend.select("relay_jobs", filters=filters, limit=1)
        return rows[0] if rows else None

    async def update_job(
        self,
        job_id: str | UUID,
        values: dict[str, Any],
        *,
        lease_owner: str | None = None,
        lease_fence: int | None = None,
    ) -> dict[str, Any] | None:
        filters = {"id": f"eq.{job_id}"}
        if lease_owner:
            filters["lease_owner"] = f"eq.{lease_owner}"
        if lease_fence is not None:
            filters["lease_fence"] = f"eq.{lease_fence}"
        rows = await self.backend.update("relay_jobs", values, filters=filters)
        return rows[0] if rows else None

    async def claim_job(self, worker_id: str) -> dict[str, Any] | None:
        result = await self.backend.rpc(
            "claim_relay_job_core",
            {
                "p_worker_id": worker_id,
                "p_lease_seconds": self.settings.job_lease_seconds,
            },
        )
        if isinstance(result, list):
            return result[0] if result else None
        return result if isinstance(result, dict) else None

    async def renew_lease(
        self, job_id: str, worker_id: str, lease_fence: int
    ) -> bool:
        result = await self.backend.rpc(
            "renew_relay_job_lease_v2",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_lease_fence": lease_fence,
                "p_lease_seconds": self.settings.job_lease_seconds,
            },
        )
        return bool(result)

    async def reserve_session_job(
        self,
        *,
        session_id: str,
        job_id: str,
        expected_history_version: int,
    ) -> bool:
        result = await self.backend.rpc(
            "reserve_relay_session_job",
            {
                "p_session_id": session_id,
                "p_job_id": job_id,
                "p_expected_history_version": expected_history_version,
            },
        )
        return bool(result)

    async def release_session_job(self, *, session_id: str, job_id: str) -> bool:
        result = await self.backend.rpc(
            "release_relay_session_job",
            {"p_session_id": session_id, "p_job_id": job_id},
        )
        return bool(result)

    async def commit_session_and_job_success(
        self,
        *,
        session_id: str,
        job_id: str,
        expected_history_version: int,
        history_object_path: str,
        provider: str,
        model: str,
        lease_owner: str,
        lease_fence: int,
        raw_response_object_path: str,
        response_output_object_path: str,
        compact_result: dict[str, Any],
        provider_response_id: str | None,
    ) -> bool:
        result = await self.backend.rpc(
            "commit_relay_session_and_job_success",
            {
                "p_session_id": session_id,
                "p_job_id": job_id,
                "p_expected_history_version": expected_history_version,
                "p_history_object_path": history_object_path,
                "p_provider": provider,
                "p_model": model,
                "p_lease_owner": lease_owner,
                "p_lease_fence": lease_fence,
                "p_raw_response_object_path": raw_response_object_path,
                "p_response_output_object_path": response_output_object_path,
                "p_compact_result": compact_result,
                "p_provider_response_id": provider_response_id,
            },
        )
        return bool(result)

    # Legacy session CAS is retained for already-created core-legacy jobs.
    async def commit_session_history(
        self,
        *,
        session_id: str,
        expected_history_version: int,
        history_object_path: str,
        provider: str,
        model: str,
    ) -> bool:
        result = await self.backend.rpc(
            "commit_relay_session_history",
            {
                "p_session_id": session_id,
                "p_expected_history_version": expected_history_version,
                "p_history_object_path": history_object_path,
                "p_provider": provider,
                "p_model": model,
            },
        )
        return bool(result)

    def default_job_expiry(self):
        return utcnow() + timedelta(seconds=self.settings.job_ttl_seconds)

    def default_session_expiry(self):
        return utcnow() + timedelta(seconds=self.settings.session_ttl_seconds)
