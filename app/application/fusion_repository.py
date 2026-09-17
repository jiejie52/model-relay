from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from ..config import Settings
from ..supabase import SupabaseBackend
from ..utils import utcnow


class FusionRepository:
    """Persistence owned by the legacy Fusion application runtime.

    This module is deliberately outside Relay Core. It may use Relay job rows as
    a compatibility transport, but Core Repository does not expose Fusion tables.
    """

    def __init__(self, backend: SupabaseBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings

    async def claim_job(self, worker_id: str) -> dict[str, Any] | None:
        result = await self.backend.rpc(
            "claim_relay_job_fusion_legacy",
            {
                "p_worker_id": worker_id,
                "p_lease_seconds": self.settings.job_lease_seconds,
            },
        )
        if isinstance(result, list):
            return result[0] if result else None
        return result if isinstance(result, dict) else None

    async def renew_lease(self, job_id: str, worker_id: str, lease_fence: int) -> bool:
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

    async def get_job(self, job_id: str | UUID) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "relay_jobs", filters={"id": f"eq.{job_id}"}, limit=1
        )
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

    def default_job_expiry(self):
        return utcnow() + timedelta(seconds=self.settings.job_ttl_seconds)

    async def create_fusion_corpus(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.upsert("fusion_corpora", row, on_conflict="id")
        return rows[0]

    async def update_fusion_corpus(
        self, corpus_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        rows = await self.backend.update(
            "fusion_corpora", values, filters={"id": f"eq.{corpus_id}"}
        )
        return rows[0] if rows else None

    async def get_fusion_corpus(
        self,
        corpus_id: str,
        *,
        tenant_id: str | None = None,
        conversation_hash: str | None = None,
    ) -> dict[str, Any] | None:
        filters = {"id": f"eq.{corpus_id}"}
        if tenant_id:
            filters["tenant_id"] = f"eq.{tenant_id}"
        if conversation_hash:
            filters["conversation_hash"] = f"eq.{conversation_hash}"
        rows = await self.backend.select("fusion_corpora", filters=filters, limit=1)
        return rows[0] if rows else None

    async def create_fusion_material(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.upsert("fusion_materials", row, on_conflict="id")
        return rows[0]

    async def create_fusion_artifact(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.insert("fusion_artifacts", row)
        return rows[0]

    async def get_fusion_artifact(
        self,
        artifact_id: str,
        *,
        corpus_id: str | None = None,
    ) -> dict[str, Any] | None:
        filters = {"id": f"eq.{artifact_id}"}
        if corpus_id:
            filters["fusion_corpus_id"] = f"eq.{corpus_id}"
        rows = await self.backend.select("fusion_artifacts", filters=filters, limit=1)
        return rows[0] if rows else None

    async def next_fusion_artifact_version(self, corpus_id: str, artifact_type: str) -> int:
        rows = await self.backend.select(
            "fusion_artifacts",
            filters={
                "fusion_corpus_id": f"eq.{corpus_id}",
                "artifact_type": f"eq.{artifact_type}",
            },
            select="artifact_version",
            limit=1,
            order="artifact_version.desc",
        )
        if not rows:
            return 1
        try:
            return int(rows[0].get("artifact_version") or 0) + 1
        except Exception:
            return 1
