from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .config import Settings
from .core_models import SessionCreateRequest, SessionJobRequest
from .error_contract import dependency_http_error_meta, encode_inline_body, relay_error_meta
from .providers.base import ProviderRequestError
from .providers.registry import ProviderRegistry
from .repository import RelayRepository
from .storage_paths import job_object_path, session_context_path
from .supabase import SupabaseBackend, SupabaseError
from .utils import json_bytes, stable_prompt_cache_key, utcnow


class CoreError(RuntimeError):
    status_code = 400

    def __init__(
        self,
        code: str,
        message: str,
        *,
        error_meta: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.error_meta = error_meta


class CoreNotFound(CoreError):
    status_code = 404


class CoreConflict(CoreError):
    status_code = 409


class CoreDependencyError(CoreError):
    status_code = 502


def canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class RelayCoreService:
    def __init__(
        self,
        backend: SupabaseBackend,
        repo: RelayRepository,
        providers: ProviderRegistry,
        settings: Settings,
    ) -> None:
        self.backend = backend
        self.repo = repo
        self.providers = providers
        self.settings = settings

    def _profile(self, provider: str, requested: str) -> tuple[str, str, str]:
        canonical = self.providers.canonical_provider(provider)
        adapter = self.providers.get(canonical)
        value = str(requested or "default").strip().lower()
        if canonical == "moonshot":
            if value not in {"default", "moonshot-official"}:
                raise CoreError(
                    "UPSTREAM_PROFILE_UNSUPPORTED",
                    "Moonshot sessions only accept upstream_profile=moonshot-official",
                )
            return "moonshot-official", adapter.protocol, adapter.history_codec
        if value not in {"default", "aihubmix"}:
            raise CoreError(
                "UPSTREAM_PROFILE_UNSUPPORTED",
                "This provider is currently configured through upstream_profile=aihubmix",
            )
        return "aihubmix", adapter.protocol, adapter.history_codec

    async def create_session(
        self,
        request: SessionCreateRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        profile, protocol, history_codec = self._profile(
            request.provider, request.upstream_profile
        )
        provider = self.providers.canonical_provider(request.provider)
        fingerprint = canonical_hash(
            {
                "tenant_id": request.tenant_id,
                "conversation_hash": request.conversation_hash,
                "provider": provider,
                "upstream_profile": profile,
                "protocol": protocol,
                "history_codec": history_codec,
                "history_mode": request.history_mode,
                "defaults": request.defaults,
                "context_identity": request.context_identity,
                "context_hash": canonical_hash(request.context),
                "metadata": request.metadata,
            }
        )

        existing = await self.repo.find_session_by_idempotency(
            request.tenant_id,
            request.conversation_hash,
            idempotency_key,
        )
        if existing:
            if existing.get("request_fingerprint") != fingerprint:
                raise CoreConflict(
                    "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST",
                    "The session idempotency key already identifies a different request",
                )
            return existing

        # Deterministic only within the owner+idempotency namespace. Concurrent
        # retries therefore write the same context object and compete on the same
        # Session primary key instead of leaving orphan Sessions/objects.
        session_id = uuid5(
            NAMESPACE_URL,
            f"relay-v2-session:{request.tenant_id}:{request.conversation_hash}:{idempotency_key}",
        )
        context_path = session_context_path(
            self.settings,
            request.tenant_id,
            request.conversation_hash,
            str(session_id),
        )
        await self.backend.storage_put(
            context_path,
            json_bytes(request.context),
            content_type="application/json",
        )

        requested_expiry = request.context_expires_at or self.repo.default_session_expiry()
        session_expiry = min(requested_expiry, self.repo.default_session_expiry())
        model = request.defaults.get("model")
        row = {
            "id": str(session_id),
            "tenant_id": request.tenant_id,
            "conversation_hash": request.conversation_hash,
            "provider": provider,
            "model": str(model) if model else "",
            "prompt_cache_key": stable_prompt_cache_key(
                f"{request.conversation_hash}:{session_id}"
            ),
            # Legacy reader compatibility.
            "material_prefix_object_path": context_path,
            "context_object_path": context_path,
            "context_identity": request.context_identity,
            "history_object_path": None,
            "signed_url_expires_at": request.context_expires_at.isoformat()
            if request.context_expires_at
            else None,
            "history_version": 0,
            "history_mode": request.history_mode,
            "upstream_profile_id": profile,
            "protocol": protocol,
            "history_codec": history_codec,
            "active_job_id": None,
            "idempotency_key": idempotency_key,
            "request_fingerprint": fingerprint,
            "metadata": request.metadata,
            "expires_at": session_expiry.isoformat(),
        }
        try:
            return await self.repo.create_session(row)
        except SupabaseError as exc:
            if exc.status_code == 409:
                existing = await self.repo.find_session_by_idempotency(
                    request.tenant_id,
                    request.conversation_hash,
                    idempotency_key,
                )
                if existing and existing.get("request_fingerprint") == fingerprint:
                    return existing
            meta = dependency_http_error_meta(
                service="supabase",
                http_status=exc.status_code,
                response_headers=exc.headers,
                body=exc.body,
            )
            meta["exception"] = {"type": type(exc).__name__, "message": str(exc)}
            raise CoreDependencyError(
                "SESSION_PERSIST_FAILED",
                "Failed to persist Relay session",
                error_meta=meta,
            ) from exc

    async def get_session(
        self, session_id: UUID, *, tenant_id: str, conversation_hash: str
    ) -> dict[str, Any]:
        row = await self.repo.get_session(
            session_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        if not row:
            raise CoreNotFound("SESSION_NOT_FOUND", "Relay session not found")
        return row

    async def submit_session_job(
        self,
        session_id: UUID,
        request: SessionJobRequest,
        *,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session = await self.get_session(
            session_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        if self._expired(session.get("expires_at")):
            raise CoreConflict("SESSION_EXPIRED", "Relay session has expired")

        provider = str(session.get("provider") or "")
        try:
            adapter = self.providers.get(provider)
        except ProviderRequestError as exc:
            raise CoreError(exc.code, exc.message) from exc
        if session.get("protocol") and session.get("protocol") != adapter.protocol:
            raise CoreConflict(
                "SESSION_PROTOCOL_MISMATCH",
                "Stored session protocol does not match the configured provider adapter",
            )
        if session.get("history_codec") and session.get("history_codec") != adapter.history_codec:
            raise CoreConflict(
                "SESSION_HISTORY_CODEC_MISMATCH",
                "Stored session history codec does not match the configured provider adapter",
            )

        # Resolve an existing idempotent job before using mutable Session state in
        # the request fingerprint. A retry of the original request must still
        # attach to its original Job even after later jobs advanced the Session.
        existing = await self.repo.find_job_by_idempotency(
            tenant_id, conversation_hash, idempotency_key
        )
        model = str(
            request.model
            or ((existing or {}).get("model") if existing else None)
            or session.get("model")
            or ""
        ).strip()
        if not model:
            raise CoreError("MODEL_REQUIRED", "No model is configured for this session job")

        history_mode = str(session.get("history_mode") or "append")
        existing_expected_version = (
            existing.get("expected_history_version") if existing else None
        )
        expected_history_version = int(
            existing_expected_version
            if existing_expected_version is not None
            else session.get("history_version") or 0
        )
        capability_profile_version = (
            str(existing.get("capability_profile_version"))
            if existing and existing.get("capability_profile_version")
            else (None if existing else adapter.capability_profile_version(model))
        )
        fingerprint_payload = {
            "session_id": str(session_id),
            "provider": provider,
            "upstream_profile": session.get("upstream_profile_id"),
            "protocol": session.get("protocol"),
            "history_codec": session.get("history_codec"),
            "history_mode": history_mode,
            "expected_history_version": expected_history_version,
            "model": model,
            "input": request.input,
            "generation": request.generation,
            "provider_payload": request.provider_payload,
            "structured_output": request.structured_output,
            "metadata": request.metadata,
            "label": request.label,
        }
        # Historical rows created before capability profile snapshots did not have
        # this field in their fingerprint. Do not make a software upgrade turn a
        # safe replay into a false idempotency conflict.
        if capability_profile_version is not None:
            fingerprint_payload["capability_profile_version"] = capability_profile_version
        fingerprint = canonical_hash(fingerprint_payload)
        if existing:
            if existing.get("request_fingerprint") != fingerprint:
                raise CoreConflict(
                    "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST",
                    "The job idempotency key already identifies a different request",
                )
            return existing

        if capability_profile_version is None:
            capability_profile_version = adapter.capability_profile_version(model)

        job_id = uuid4()
        request_snapshot = {
            "schema_version": "relay-core-request/2.0",
            "tenant_id": tenant_id,
            "conversation_hash": conversation_hash,
            "relay_session_id": str(session_id),
            "provider": provider,
            "upstream_profile": session.get("upstream_profile_id"),
            "protocol": session.get("protocol"),
            "history_codec": session.get("history_codec"),
            "history_mode": history_mode,
            "expected_history_version": expected_history_version,
            "capability_profile_version": capability_profile_version,
            "context_object_path": session.get("context_object_path")
            or session.get("material_prefix_object_path"),
            "model": model,
            "input": request.input,
            "generation": request.generation,
            "provider_payload": request.provider_payload,
            "structured_output": request.structured_output,
            "metadata": request.metadata,
            "label": request.label,
        }
        request_path = job_object_path(
            self.settings,
            tenant_id,
            conversation_hash,
            str(job_id),
            "request.json",
        )
        await self.backend.storage_put(request_path, json_bytes(request_snapshot))

        needs_reservation = history_mode == "append"
        row = {
            "id": str(job_id),
            "tenant_id": tenant_id,
            "conversation_hash": conversation_hash,
            "relay_session_id": str(session_id),
            # DB compatibility field; Core never branches on this value.
            "stage": request.label or "session_job",
            "provider": provider,
            "status": "prepared" if needs_reservation else "queued",
            "model": model,
            "think_level": None,
            "request_object_path": request_path,
            "compact_result": None,
            "idempotency_key": idempotency_key,
            "request_fingerprint": fingerprint,
            "execution_engine": self.settings.core_execution_engine,
            "expected_history_version": expected_history_version,
            "protocol_snapshot": session.get("protocol"),
            "capability_profile_version": capability_profile_version,
            "attempt_count": 0,
            "expires_at": self.repo.default_job_expiry().isoformat(),
        }
        try:
            created = await self.repo.create_job(row)
        except SupabaseError as exc:
            if exc.status_code == 409:
                existing = await self.repo.find_job_by_idempotency(
                    tenant_id, conversation_hash, idempotency_key
                )
                if existing and existing.get("request_fingerprint") == fingerprint:
                    return existing
            meta = dependency_http_error_meta(
                service="supabase",
                http_status=exc.status_code,
                response_headers=exc.headers,
                body=exc.body,
            )
            meta["exception"] = {"type": type(exc).__name__, "message": str(exc)}
            raise CoreDependencyError(
                "JOB_PERSIST_FAILED",
                "Failed to persist Relay job",
                error_meta=meta,
            ) from exc

        if needs_reservation:
            reserved = await self.repo.reserve_session_job(
                session_id=str(session_id),
                job_id=str(job_id),
                expected_history_version=expected_history_version,
            )
            if not reserved:
                meta = relay_error_meta(
                    "SESSION_BUSY_OR_VERSION_CONFLICT",
                    "Another append job is active or the session history version changed",
                )
                await self.repo.update_job(
                    job_id,
                    {
                        "status": "failed",
                        "error_code": meta["relay_code"],
                        "error_message": meta["relay_message"],
                        "raw_error_meta": meta,
                        "completed_at": utcnow().isoformat(),
                    },
                )
                raise CoreConflict(meta["relay_code"], meta["relay_message"])
            queued = await self.repo.update_job(job_id, {"status": "queued"})
            if not queued:
                await self.repo.release_session_job(
                    session_id=str(session_id), job_id=str(job_id)
                )
                raise CoreDependencyError(
                    "JOB_QUEUE_FAILED", "Failed to transition prepared job to queued"
                )
            created = queued
        return created

    async def get_job(
        self,
        session_id: UUID,
        job_id: UUID,
        *,
        tenant_id: str,
        conversation_hash: str,
    ) -> dict[str, Any]:
        job = await self.repo.get_job(
            job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        if not job or str(job.get("relay_session_id") or "") != str(session_id):
            raise CoreNotFound("JOB_NOT_FOUND", "Relay job not found")
        return job

    async def cancel_job(
        self,
        session_id: UUID,
        job_id: UUID,
        *,
        tenant_id: str,
        conversation_hash: str,
    ) -> dict[str, Any]:
        job = await self.get_job(
            session_id,
            job_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        if job.get("status") in {"succeeded", "failed", "cancelled", "expired"}:
            return job
        updated = await self.repo.update_job(
            job_id,
            {
                "status": "cancelled",
                "completed_at": utcnow().isoformat(),
                "error_code": "CANCELLED_BY_CLIENT",
                "error_message": "Job cancelled by client",
                "raw_error_meta": relay_error_meta(
                    "CANCELLED_BY_CLIENT", "Job cancelled by client"
                ),
            },
        )
        if job.get("relay_session_id"):
            await self.repo.release_session_job(
                session_id=str(job["relay_session_id"]), job_id=str(job_id)
            )
        return updated or job

    async def raw_error_bytes(self, job: dict[str, Any]) -> bytes | None:
        path = job.get("raw_error_object_path")
        if not path:
            return None
        return await self.backend.storage_get(path)

    async def error_view(self, job: dict[str, Any]) -> dict[str, Any] | None:
        meta = job.get("raw_error_meta")
        if not isinstance(meta, dict):
            if job.get("error_code") or job.get("error_message"):
                meta = relay_error_meta(
                    str(job.get("error_code") or "RELAY_ERROR"),
                    str(job.get("error_message") or ""),
                )
            else:
                return None
        out = dict(meta)
        raw = await self.raw_error_bytes(job)
        if raw is not None:
            out["body_ref"] = "raw-error"
            out["byte_length"] = len(raw)
            out["sha256"] = hashlib.sha256(raw).hexdigest()
            if len(raw) <= self.settings.relay_raw_error_inline_limit_bytes:
                out["body"] = encode_inline_body(raw)
        return out

    @staticmethod
    def _expired(value: str | None) -> bool:
        if not value:
            return False
        from datetime import datetime

        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return False
        return dt <= utcnow()
