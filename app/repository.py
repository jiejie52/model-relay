from datetime import timedelta
from typing import Any
from uuid import UUID

from .config import Settings
from .supabase import SupabaseBackend
from .utils import utcnow


class RelayRepository:
    def __init__(self, backend: SupabaseBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings

    async def find_job_by_idempotency(
        self, tenant_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "relay_jobs",
            filters={
                "tenant_id": f"eq.{tenant_id}",
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
    ) -> dict[str, Any] | None:
        filters = {"id": f"eq.{job_id}"}
        if lease_owner:
            filters["lease_owner"] = f"eq.{lease_owner}"
        rows = await self.backend.update("relay_jobs", values, filters=filters)
        return rows[0] if rows else None

    async def claim_job(self, worker_id: str) -> dict[str, Any] | None:
        result = await self.backend.rpc(
            "claim_relay_job",
            {
                "p_worker_id": worker_id,
                "p_lease_seconds": self.settings.job_lease_seconds,
            },
        )
        if isinstance(result, list):
            return result[0] if result else None
        return result if isinstance(result, dict) else None

    async def renew_lease(self, job_id: str, worker_id: str) -> bool:
        result = await self.backend.rpc(
            "renew_relay_job_lease",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_lease_seconds": self.settings.job_lease_seconds,
            },
        )
        return bool(result)

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

    # ---------------------------- V2 generic core ----------------------------

    async def reserve_material_v2(
        self,
        *,
        material_id: str,
        ingestion_id: str,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
        request_fingerprint: str,
        filename: str,
        declared_mime: str | None,
        expected_sha256: str | None,
        expected_size: int | None,
        expires_at: str,
        source_identity: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "reserve_relay_material_v2",
            {
                "p_material_id": material_id,
                "p_ingestion_id": ingestion_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_idempotency_key": idempotency_key,
                "p_request_fingerprint": request_fingerprint,
                "p_filename": filename,
                "p_declared_mime": declared_mime,
                "p_expected_sha256": expected_sha256,
                "p_expected_size": expected_size,
                "p_expires_at": expires_at,
                "p_source_identity": source_identity,
            },
        )
        return result or {}

    async def publish_material_ready_v2(
        self,
        *,
        material_id: str,
        ingestion_id: str,
        tenant_id: str,
        conversation_hash: str,
        generation: int,
        object_key: str,
        sha256: str,
        byte_length: int,
        detected_mime: str | None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "publish_relay_material_ready_v2",
            {
                "p_material_id": material_id,
                "p_ingestion_id": ingestion_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_generation": generation,
                "p_object_key": object_key,
                "p_sha256": sha256,
                "p_byte_length": byte_length,
                "p_detected_mime": detected_mime,
                "p_lease_token": lease_token,
            },
        )
        return result or {}

    async def fail_material_ingestion_v2(
        self,
        *,
        material_id: str,
        ingestion_id: str,
        tenant_id: str,
        conversation_hash: str,
        error_id: str | None,
        phase: str,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "fail_relay_material_ingestion_v2",
            {
                "p_material_id": material_id,
                "p_ingestion_id": ingestion_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_error_id": error_id,
                "p_phase": phase,
                "p_lease_token": lease_token,
            },
        )
        return result or {}

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

    async def get_materials_for_owner(
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

    async def create_session_v2(
        self,
        *,
        session_id: str,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
        request_fingerprint: str,
        provider: str,
        upstream_profile: str,
        account_scope: str,
        protocol: str,
        history_codec: str,
        history_codec_version: str,
        history_mode: str,
        model: str,
        context_object_path: str,
        context_hash: str,
        material_set_hash: str,
        material_ids: list[str],
        capability_profile_version: str,
        expires_at: str,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "create_relay_session_v2",
            {
                "p_session_id": session_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_idempotency_key": idempotency_key,
                "p_request_fingerprint": request_fingerprint,
                "p_provider": provider,
                "p_upstream_profile": upstream_profile,
                "p_account_scope": account_scope,
                "p_protocol": protocol,
                "p_history_codec": history_codec,
                "p_history_codec_version": history_codec_version,
                "p_history_mode": history_mode,
                "p_model": model,
                "p_context_object_path": context_object_path,
                "p_context_hash": context_hash,
                "p_material_set_hash": material_set_hash,
                "p_material_ids": material_ids,
                "p_capability_profile_version": capability_profile_version,
                "p_expires_at": expires_at,
            },
        )
        return result or {}

    async def get_session_materials(self, session_id: str) -> list[dict[str, Any]]:
        links = await self.backend.select(
            "relay_session_materials",
            filters={"session_id": f"eq.{session_id}"},
            order="ordinal.asc",
        )
        result: list[dict[str, Any]] = []
        for link in links:
            material = await self.get_material(str(link["material_id"]))
            if material:
                material = dict(material)
                material["ordinal"] = int(link.get("ordinal") or 0)
                result.append(material)
        return result

    async def submit_session_job_v2(
        self,
        *,
        job_id: str,
        session_id: str,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
        job_idempotency_key: str,
        request_fingerprint: str,
        request_object_path: str,
        provider: str,
        model: str,
        expected_history_version: int,
        capability_version: str,
        expires_at: str,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "submit_relay_session_job_v2",
            {
                "p_job_id": job_id,
                "p_session_id": session_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_idempotency_key": idempotency_key,
                "p_job_idempotency_key": job_idempotency_key,
                "p_request_fingerprint": request_fingerprint,
                "p_request_object_path": request_object_path,
                "p_provider": provider,
                "p_model": model,
                "p_expected_history_version": expected_history_version,
                "p_capability_version": capability_version,
                "p_expires_at": expires_at,
            },
        )
        return result or {}

    async def claim_job_v2(self, worker_id: str) -> dict[str, Any] | None:
        result = await self.backend.rpc(
            "claim_relay_job_v2",
            {
                "p_worker_id": worker_id,
                "p_engine": self.settings.execution_engine,
                "p_lease_seconds": self.settings.job_lease_seconds,
            },
        )
        if isinstance(result, list):
            return result[0] if result else None
        return result if isinstance(result, dict) else None

    async def renew_lease_v2(self, job_id: str, worker_id: str, lease_token: str) -> bool:
        result = await self.backend.rpc(
            "renew_relay_job_lease_v2",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_lease_token": lease_token,
                "p_engine": self.settings.execution_engine,
                "p_lease_seconds": self.settings.job_lease_seconds,
            },
        )
        return bool(result)

    async def update_job_v2_fenced(
        self,
        job_id: str,
        values: dict[str, Any],
        *,
        worker_id: str,
        lease_token: str,
    ) -> dict[str, Any] | None:
        rows = await self.backend.update(
            "relay_jobs",
            values,
            filters={
                "id": f"eq.{job_id}",
                "execution_engine": f"eq.{self.settings.execution_engine}",
                "lease_owner": f"eq.{worker_id}",
                "lease_token": f"eq.{lease_token}",
            },
        )
        return rows[0] if rows else None

    async def commit_job_result_v2(
        self,
        *,
        job_id: str,
        session_id: str,
        tenant_id: str,
        conversation_hash: str,
        worker_id: str,
        lease_token: str,
        expected_history_version: int,
        history_object_path: str | None,
        raw_response_object_path: str,
        response_output_object_path: str,
        compact_result: dict[str, Any],
        wire_request_hash: str | None,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "commit_relay_job_result_v2",
            {
                "p_job_id": job_id,
                "p_session_id": session_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_worker_id": worker_id,
                "p_lease_token": lease_token,
                "p_engine": self.settings.execution_engine,
                "p_expected_history_version": expected_history_version,
                "p_history_object_path": history_object_path,
                "p_raw_response_object_path": raw_response_object_path,
                "p_response_output_object_path": response_output_object_path,
                "p_compact_result": compact_result,
                "p_wire_request_hash": wire_request_hash,
            },
        )
        return result or {}

    async def record_job_failure_v2(
        self,
        *,
        job_id: str,
        session_id: str | None,
        tenant_id: str,
        conversation_hash: str,
        worker_id: str,
        lease_token: str,
        error_id: str | None,
        error_code: str | None,
        error_message: str | None,
        execution_phase: str,
        delivery_status: str,
        raw_response_object_path: str | None = None,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "record_relay_job_failure_v2",
            {
                "p_job_id": job_id,
                "p_session_id": session_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_worker_id": worker_id,
                "p_lease_token": lease_token,
                "p_engine": self.settings.execution_engine,
                "p_error_id": error_id,
                "p_error_code": error_code,
                "p_error_message": error_message,
                "p_execution_phase": execution_phase,
                "p_delivery_status": delivery_status,
                "p_raw_response_object_path": raw_response_object_path,
            },
        )
        return result or {}

    async def cancel_job_v2(
        self,
        *,
        job_id: str,
        session_id: str,
        tenant_id: str,
        conversation_hash: str,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "cancel_relay_job_v2",
            {
                "p_job_id": job_id,
                "p_session_id": session_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_engine": self.settings.execution_engine,
            },
        )
        return result or {}

    async def create_raw_error(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.insert("relay_errors", row)
        return rows[0]

    async def get_raw_error(
        self,
        error_id: str,
        *,
        tenant_id: str,
        conversation_hash: str,
    ) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "relay_errors",
            filters={
                "id": f"eq.{error_id}",
                "tenant_id": f"eq.{tenant_id}",
                "conversation_hash": f"eq.{conversation_hash}",
            },
            limit=1,
        )
        return rows[0] if rows else None

    async def get_session_material_ids(
        self,
        session_id: str,
    ) -> list[str]:
        rows = await self.backend.select(
            "relay_session_materials",
            filters={"session_id": f"eq.{session_id}"},
            select="material_id,ordinal",
            order="ordinal.asc",
        )
        return [str(row["material_id"]) for row in rows if row.get("material_id")]

    async def get_material_binding(
        self,
        *,
        material_id: str,
        provider: str,
        upstream_profile: str,
        account_scope: str,
        purpose: str,
        transform_version: str,
    ) -> dict[str, Any] | None:
        rows = await self.backend.select(
            "relay_material_bindings",
            filters={
                "material_id": f"eq.{material_id}",
                "provider": f"eq.{provider}",
                "upstream_profile": f"eq.{upstream_profile}",
                "account_scope": f"eq.{account_scope}",
                "purpose": f"eq.{purpose}",
                "transform_version": f"eq.{transform_version}",
            },
            limit=1,
        )
        return rows[0] if rows else None

    async def upsert_material_binding(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self.backend.upsert(
            "relay_material_bindings",
            row,
            on_conflict="material_id,provider,upstream_profile,account_scope,purpose,transform_version",
        )
        return rows[0]

    async def reserve_material_binding_v2(
        self,
        *,
        material_id: str,
        tenant_id: str,
        conversation_hash: str,
        provider: str,
        upstream_profile: str,
        account_scope: str,
        protocol: str,
        capability_group: str | None,
        purpose: str,
        transform_version: str,
        lease_owner: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "reserve_relay_material_binding_v2",
            {
                "p_material_id": material_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_provider": provider,
                "p_upstream_profile": upstream_profile,
                "p_account_scope": account_scope,
                "p_protocol": protocol,
                "p_capability_group": capability_group,
                "p_purpose": purpose,
                "p_transform_version": transform_version,
                "p_lease_owner": lease_owner,
                "p_lease_seconds": lease_seconds,
            },
        )
        return result or {}

    async def complete_material_binding_v2(
        self,
        *,
        binding_id: str,
        lease_owner: str,
        lease_token: str,
        native_file_id: str | None = None,
        native_uri: str | None = None,
        derived_object_path: str | None = None,
        expires_at: str | None = None,
        transport_epoch: int | None = None,
        wire_request_hash: str | None = None,
        cache_identity: str | None = None,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "complete_relay_material_binding_v2",
            {
                "p_binding_id": binding_id,
                "p_lease_owner": lease_owner,
                "p_lease_token": lease_token,
                "p_native_file_id": native_file_id,
                "p_native_uri": native_uri,
                "p_derived_object_path": derived_object_path,
                "p_expires_at": expires_at,
                "p_transport_epoch": transport_epoch,
                "p_wire_request_hash": wire_request_hash,
                "p_cache_identity": cache_identity,
            },
        )
        return result or {}

    async def fail_material_binding_v2(
        self,
        *,
        binding_id: str,
        lease_owner: str,
        lease_token: str,
        error_id: str | None = None,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "fail_relay_material_binding_v2",
            {
                "p_binding_id": binding_id,
                "p_lease_owner": lease_owner,
                "p_lease_token": lease_token,
                "p_error_id": error_id,
            },
        )
        return result or {}

    async def retry_material_v2(
        self,
        *,
        material_id: str,
        ingestion_id: str,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "retry_relay_material_v2",
            {
                "p_material_id": material_id,
                "p_ingestion_id": ingestion_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
                "p_idempotency_key": idempotency_key,
                "p_request_fingerprint": request_fingerprint,
            },
        )
        return result or {}

    async def request_material_delete_v2(
        self,
        *,
        material_id: str,
        tenant_id: str,
        conversation_hash: str,
    ) -> dict[str, Any]:
        result = await self.backend.rpc(
            "request_relay_material_delete_v2",
            {
                "p_material_id": material_id,
                "p_tenant_id": tenant_id,
                "p_conversation_hash": conversation_hash,
            },
        )
        return result or {}
