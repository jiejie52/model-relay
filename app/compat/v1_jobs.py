from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from ..config import Settings
from ..core_service import RelayCoreService, canonical_hash
from ..error_contract import relay_error_meta
from ..providers.base import ProviderRequestError
from ..repository import RelayRepository
from ..security import require_owner_headers, require_relay_auth
from ..storage_paths import job_object_path, session_material_prefix_path
from ..supabase import SupabaseBackend, SupabaseError
from ..utils import json_bytes, stable_prompt_cache_key, truncate_utf8, utcnow
from .models_v1 import CancelResponse, FUSION_STAGES, JobStatusResponse, JobSubmitRequest, JobSubmitResponse


router = APIRouter(tags=["compat-v1-jobs"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _repo(request: Request) -> RelayRepository:
    return request.app.state.repo


def _backend(request: Request) -> SupabaseBackend:
    return request.app.state.backend


def _core(request: Request) -> RelayCoreService:
    return request.app.state.core_service


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _submit_response(job: dict[str, Any]) -> JobSubmitResponse:
    return JobSubmitResponse(
        job_id=UUID(job["id"]),
        relay_session_id=UUID(job["relay_session_id"]) if job.get("relay_session_id") else None,
        status=job["status"],
        poll_after_seconds=5,
        submitted_at=_parse_dt(job.get("created_at")),
        expires_at=_parse_dt(job.get("expires_at")),
    )


def _is_session_expiring(session: dict[str, Any], settings: Settings) -> bool:
    signed = _parse_dt(session.get("signed_url_expires_at"))
    if signed is None:
        return False
    return signed <= utcnow() + timedelta(seconds=settings.session_expiry_safety_seconds)


def _profile(provider: str, request: Request) -> tuple[str, str, str, str]:
    registry = request.app.state.providers
    try:
        canonical = registry.canonical_provider(provider)
        adapter = registry.get(canonical)
    except ProviderRequestError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    profile = "moonshot-official" if canonical == "moonshot" else "aihubmix"
    return canonical, profile, adapter.protocol, adapter.history_codec


@router.post(
    "/v1/jobs",
    response_model=JobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_relay_auth)],
)
async def submit_job(
    body: JobSubmitRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
) -> JobSubmitResponse:
    repository = _repo(request)
    store = _backend(request)
    settings = _settings(request)

    canonical_provider, profile, protocol, history_codec = _profile(body.provider, request)
    # The legacy contract may contain upstream.base_url, but the compatibility
    # adapter does not allow it to redirect server credentials to an arbitrary host.
    if body.upstream and body.upstream.base_url:
        configured = settings.moonshot_root if canonical_provider == "moonshot" else settings.aihubmix_root
        if body.upstream.base_url.rstrip("/") != configured:
            raise HTTPException(
                status_code=400,
                detail="Legacy upstream.base_url must match the server-configured provider profile",
            )

    existing = await repository.find_job_by_idempotency(
        body.tenant_id, body.conversation_hash, idempotency_key
    )

    # Compatibility idempotency is checked before any generated Session/object is
    # created. The fingerprint uses caller-visible semantics plus the original
    # history version so a replay attaches to the same logical Job without
    # producing an orphan Session.
    fingerprint_base = body.model_dump(mode="json")
    fingerprint_base["provider"] = canonical_provider
    fingerprint_base["upstream_profile"] = profile
    fingerprint_base["protocol"] = protocol
    fingerprint_base["history_codec"] = history_codec
    if existing:
        fingerprint_base["expected_history_version"] = int(
            existing.get("expected_history_version") or 0
        )
        replay_fingerprint = canonical_hash(fingerprint_base)
        existing_fp = existing.get("request_fingerprint")
        if existing_fp and existing_fp != replay_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST",
            )
        return _submit_response(existing)

    session_id: UUID | None = None
    session: dict[str, Any] | None = None
    material_prefix_path: str | None = None
    expected_history_version = 0

    if body.mode == "continue_session":
        session = await repository.get_session(
            body.relay_session_id,
            tenant_id=body.tenant_id,
            conversation_hash=body.conversation_hash,
        )
        if not session:
            raise HTTPException(status_code=404, detail="Relay session not found")
        if str(session.get("provider") or "") != canonical_provider:
            raise HTTPException(
                status_code=409,
                detail="Relay session provider cannot change across continuation",
            )
        if session.get("upstream_profile_id") and session.get("upstream_profile_id") != profile:
            raise HTTPException(status_code=409, detail="Relay session upstream profile cannot change")
        if session.get("protocol") and session.get("protocol") != protocol:
            raise HTTPException(status_code=409, detail="Relay session protocol cannot change")
        if _is_session_expiring(session, settings):
            raise HTTPException(status_code=409, detail="RELAY_SESSION_MATERIAL_EXPIRING")
        session_id = UUID(session["id"])
        material_prefix_path = session.get("material_prefix_object_path")
        expected_history_version = int(session.get("history_version") or 0)

    elif body.mode == "new_session":
        session_id = uuid5(
            NAMESPACE_URL,
            f"relay-v1-session:{body.tenant_id}:{body.conversation_hash}:{idempotency_key}",
        )
        material_prefix_path = session_material_prefix_path(
            settings,
            body.tenant_id,
            body.conversation_hash,
            str(session_id),
        )
        await store.storage_put(
            material_prefix_path,
            json_bytes(body.material_prefix),
            content_type="application/json",
        )
        signed_expiry = body.material_expires_at or (
            utcnow() + timedelta(seconds=settings.supabase_signed_url_ttl)
        )
        session_expiry = min(
            signed_expiry,
            utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        )
        session = await repository.create_session(
            {
                "id": str(session_id),
                "tenant_id": body.tenant_id,
                "conversation_hash": body.conversation_hash,
                "provider": canonical_provider,
                "model": body.model,
                "prompt_cache_key": stable_prompt_cache_key(body.conversation_hash),
                "material_prefix_object_path": material_prefix_path,
                "context_object_path": material_prefix_path,
                "history_object_path": None,
                "signed_url_expires_at": signed_expiry.isoformat(),
                "history_version": 0,
                "history_mode": "append",
                "upstream_profile_id": profile,
                "protocol": protocol,
                "history_codec": history_codec,
                "active_job_id": None,
                "expires_at": session_expiry.isoformat(),
            }
        )

    fingerprint_base["expected_history_version"] = expected_history_version
    fingerprint = canonical_hash(fingerprint_base)

    snapshot = body.model_dump(mode="json")
    snapshot["provider"] = canonical_provider
    snapshot["relay_session_id"] = str(session_id) if session_id else None
    snapshot["expected_history_version"] = expected_history_version
    snapshot["material_prefix_object_path"] = material_prefix_path
    snapshot["upstream_profile"] = profile
    snapshot["protocol"] = protocol
    snapshot["history_codec"] = history_codec
    if body.mode in {"new_session", "continue_session"}:
        snapshot["material_prefix"] = None

    job_id = uuid4()
    request_path = job_object_path(
        settings,
        body.tenant_id,
        body.conversation_hash,
        str(job_id),
        "request.json",
    )
    await store.storage_put(request_path, json_bytes(snapshot))

    is_fusion = body.stage in FUSION_STAGES
    needs_reservation = bool(session_id and not is_fusion)
    engine = (
        settings.legacy_fusion_execution_engine
        if is_fusion
        else settings.legacy_core_execution_engine
    )
    row = {
        "id": str(job_id),
        "tenant_id": body.tenant_id,
        "conversation_hash": body.conversation_hash,
        "relay_session_id": str(session_id) if session_id else None,
        "stage": body.stage,
        "provider": canonical_provider,
        "status": "prepared" if needs_reservation else "queued",
        "model": body.model,
        "think_level": body.think_level,
        "request_object_path": request_path,
        "compact_result": None,
        "idempotency_key": idempotency_key,
        "request_fingerprint": fingerprint,
        "execution_engine": engine,
        "expected_history_version": expected_history_version,
        "protocol_snapshot": protocol,
        "attempt_count": 0,
        "expires_at": repository.default_job_expiry().isoformat(),
    }
    try:
        created = await repository.create_job(row)
    except SupabaseError as exc:
        if exc.status_code == 409:
            existing = await repository.find_job_by_idempotency(
                body.tenant_id, body.conversation_hash, idempotency_key
            )
            if existing:
                return _submit_response(existing)
        raise HTTPException(status_code=502, detail="Failed to persist Relay job") from exc

    if needs_reservation:
        reserved = await repository.reserve_session_job(
            session_id=str(session_id),
            job_id=str(job_id),
            expected_history_version=expected_history_version,
        )
        if not reserved:
            meta = relay_error_meta(
                "SESSION_BUSY_OR_VERSION_CONFLICT",
                "Another session continuation is already active or history changed",
            )
            await repository.update_job(
                job_id,
                {
                    "status": "failed",
                    "error_code": meta["relay_code"],
                    "error_message": meta["relay_message"],
                    "raw_error_meta": meta,
                    "completed_at": utcnow().isoformat(),
                },
            )
            raise HTTPException(status_code=409, detail=meta["relay_code"])
        created = await repository.update_job(job_id, {"status": "queued"}) or created

    return _submit_response(created)


@router.get(
    "/v1/jobs/{job_id}",
    response_model=JobStatusResponse,
    dependencies=[Depends(require_relay_auth)],
)
async def get_job_status(
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> JobStatusResponse:
    tenant_id, conversation_hash = owner
    job = await _repo(request).get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(
        job_id=job_id,
        relay_session_id=UUID(job["relay_session_id"]) if job.get("relay_session_id") else None,
        status=job["status"],
        stage=job["stage"],
        provider=job["provider"],
        model=job["model"],
        heartbeat_at=_parse_dt(job.get("heartbeat_at")),
        started_at=_parse_dt(job.get("started_at")),
        completed_at=_parse_dt(job.get("completed_at")),
        poll_after_seconds=10,
        retryable=False,
        # Provider failures are represented by the raw error contract rather than
        # a Relay-normalized upstream code/message.
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
    )


@router.get(
    "/v1/jobs/{job_id}/result",
    dependencies=[Depends(require_relay_auth)],
)
async def get_job_result(
    job_id: UUID,
    request: Request,
    view: str = Query(default="dify"),
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> Response:
    if view != "dify":
        raise HTTPException(status_code=400, detail="Only view=dify is available on v1 compatibility API")
    tenant_id, conversation_hash = owner
    job = await _repo(request).get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    settings = _settings(request)

    if job["status"] == "succeeded":
        payload = job.get("compact_result") or {
            "job_id": str(job_id),
            "status": "failed",
            "error_code": "COMPACT_RESULT_MISSING",
        }
        raw = json_bytes(payload)
        if len(raw) > settings.relay_result_hard_limit_bytes:
            safe = dict(payload)
            safe["text"] = truncate_utf8(
                str(safe.get("text") or ""), settings.relay_result_preview_bytes
            )
            safe["text_truncated"] = True
            raw = json_bytes(safe)
        return Response(content=raw, media_type="application/json", status_code=200)

    if job["status"] in {"failed", "cancelled", "expired"}:
        error = await _core(request).error_view(job)
        payload = {
            "job_id": str(job_id),
            "status": job["status"],
            "error_code": job.get("error_code"),
            "error_message": job.get("error_message"),
            "error": error,
            "raw_error_available": bool(job.get("raw_error_object_path")),
            "raw_error_path": f"/v1/jobs/{job_id}/error/raw" if job.get("raw_error_object_path") else None,
        }
        return Response(content=json_bytes(payload), media_type="application/json", status_code=200)

    return Response(
        content=json_bytes(
            {
                "job_id": str(job_id),
                "status": job["status"],
                "stage": job["stage"],
                "poll_after_seconds": 10,
            }
        ),
        media_type="application/json",
        status_code=202,
    )


@router.get(
    "/v1/jobs/{job_id}/error/raw",
    dependencies=[Depends(require_relay_auth)],
)
async def get_raw_error(
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> Response:
    tenant_id, conversation_hash = owner
    job = await _repo(request).get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    raw = await _core(request).raw_error_bytes(job)
    if raw is None:
        raise HTTPException(status_code=404, detail="Raw provider error is not available")
    return Response(content=raw, media_type="application/octet-stream", status_code=200)


@router.post(
    "/v1/jobs/{job_id}/cancel",
    response_model=CancelResponse,
    dependencies=[Depends(require_relay_auth)],
)
async def cancel_job(
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> CancelResponse:
    tenant_id, conversation_hash = owner
    repository = _repo(request)
    job = await repository.get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] not in {"succeeded", "failed", "cancelled", "expired"}:
        meta = relay_error_meta("CANCELLED_BY_CLIENT", "Job cancelled by client")
        updated = await repository.update_job(
            job_id,
            {
                "status": "cancelled",
                "completed_at": utcnow().isoformat(),
                "error_code": meta["relay_code"],
                "error_message": meta["relay_message"],
                "raw_error_meta": meta,
            },
        )
        if updated:
            job = updated
        if job.get("relay_session_id"):
            await repository.release_session_job(
                session_id=str(job["relay_session_id"]), job_id=str(job_id)
            )

    return CancelResponse(job_id=job_id, status=job["status"])
