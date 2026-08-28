import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status

from .config import get_settings
from .models import CancelResponse, JobStatusResponse, JobSubmitRequest, JobSubmitResponse
from .repository import RelayRepository
from .security import require_owner_headers, require_relay_auth
from .storage_paths import job_object_path, session_material_prefix_path
from .supabase import SupabaseBackend, SupabaseError
from .utils import json_bytes, stable_prompt_cache_key, truncate_utf8, utcnow


settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("model-relay-api")

backend: SupabaseBackend | None = None
repo: RelayRepository | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global backend, repo
    backend = SupabaseBackend(settings)
    repo = RelayRepository(backend, settings)
    try:
        yield
    finally:
        await backend.close()


app = FastAPI(title="Model Relay API", version="0.2.0-fusion", lifespan=lifespan)


def _repo() -> RelayRepository:
    assert repo is not None
    return repo


def _backend() -> SupabaseBackend:
    assert backend is not None
    return backend


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_session_expiring(session: dict[str, Any]) -> bool:
    signed = _parse_dt(session.get("signed_url_expires_at"))
    if signed is None:
        return False
    return signed <= utcnow() + timedelta(seconds=settings.session_expiry_safety_seconds)


def _submit_response(job: dict[str, Any]) -> JobSubmitResponse:
    return JobSubmitResponse(
        job_id=UUID(job["id"]),
        relay_session_id=UUID(job["relay_session_id"]) if job.get("relay_session_id") else None,
        status=job["status"],
        poll_after_seconds=5,
        submitted_at=_parse_dt(job.get("created_at")),
        expires_at=_parse_dt(job.get("expires_at")),
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "relay-api", "version": "0.2.0-fusion"}


@app.post(
    "/v1/jobs",
    response_model=JobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_relay_auth)],
)
async def submit_job(
    request: JobSubmitRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
) -> JobSubmitResponse:
    repository = _repo()
    store = _backend()

    existing = await repository.find_job_by_idempotency(
        request.tenant_id, idempotency_key
    )
    if existing:
        return _submit_response(existing)

    session_id: UUID | None = None
    session: dict[str, Any] | None = None
    material_prefix_path: str | None = None
    expected_history_version = 0

    if request.mode == "continue_session":
        session = await repository.get_session(
            request.relay_session_id,
            tenant_id=request.tenant_id,
            conversation_hash=request.conversation_hash,
        )
        if not session:
            raise HTTPException(status_code=404, detail="Relay session not found")
        if session.get("provider") != request.provider:
            raise HTTPException(
                status_code=409,
                detail="Relay session provider cannot change across continuation",
            )
        if _is_session_expiring(session):
            raise HTTPException(
                status_code=409,
                detail="RELAY_SESSION_MATERIAL_EXPIRING",
            )
        session_id = UUID(session["id"])
        material_prefix_path = session.get("material_prefix_object_path")
        expected_history_version = int(session.get("history_version") or 0)

    elif request.mode == "new_session":
        session_id = uuid4()
        material_prefix_path = session_material_prefix_path(
            settings,
            request.tenant_id,
            request.conversation_hash,
            str(session_id),
        )
        await store.storage_put(
            material_prefix_path,
            json_bytes(request.material_prefix),
            content_type="application/json",
        )

        signed_expiry = request.material_expires_at or (
            utcnow() + timedelta(seconds=settings.supabase_signed_url_ttl)
        )
        session_expiry = min(
            signed_expiry,
            utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        )
        session = await repository.create_session(
            {
                "id": str(session_id),
                "tenant_id": request.tenant_id,
                "conversation_hash": request.conversation_hash,
                "provider": request.provider,
                "model": request.model,
                "prompt_cache_key": stable_prompt_cache_key(request.conversation_hash),
                "material_prefix_object_path": material_prefix_path,
                "history_object_path": None,
                "signed_url_expires_at": signed_expiry.isoformat(),
                "history_version": 0,
                "expires_at": session_expiry.isoformat(),
            }
        )

    job_id = uuid4()
    snapshot = request.model_dump(mode="json")
    snapshot["relay_session_id"] = str(session_id) if session_id else None
    snapshot["expected_history_version"] = expected_history_version
    snapshot["material_prefix_object_path"] = material_prefix_path

    # Avoid duplicating the immutable material prefix for session-backed jobs.
    if request.mode in {"new_session", "continue_session"}:
        snapshot["material_prefix"] = None

    request_path = job_object_path(
        settings,
        request.tenant_id,
        request.conversation_hash,
        str(job_id),
        "request.json",
    )
    await store.storage_put(request_path, json_bytes(snapshot))

    row = {
        "id": str(job_id),
        "tenant_id": request.tenant_id,
        "conversation_hash": request.conversation_hash,
        "relay_session_id": str(session_id) if session_id else None,
        "stage": request.stage,
        "provider": request.provider,
        "status": "queued",
        "model": request.model,
        "think_level": request.think_level,
        "request_object_path": request_path,
        "compact_result": None,
        "idempotency_key": idempotency_key,
        "attempt_count": 0,
        "expires_at": repository.default_job_expiry().isoformat(),
    }

    try:
        created = await repository.create_job(row)
    except SupabaseError as exc:
        # A concurrent retry may have won the idempotency race.
        if exc.status_code == 409:
            existing = await repository.find_job_by_idempotency(
                request.tenant_id, idempotency_key
            )
            if existing:
                return _submit_response(existing)
        raise HTTPException(status_code=502, detail="Failed to persist Relay job") from exc

    return _submit_response(created)


@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobStatusResponse,
    dependencies=[Depends(require_relay_auth)],
)
async def get_job_status(
    job_id: UUID,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> JobStatusResponse:
    tenant_id, conversation_hash = owner
    job = await _repo().get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    retryable = job.get("error_code") in {
        "UPSTREAM_TIMEOUT",
        "UPSTREAM_SERVER_ERROR",
        "WORKER_ERROR",
    }
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
        retryable=retryable,
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
    )


@app.get(
    "/v1/jobs/{job_id}/result",
    dependencies=[Depends(require_relay_auth)],
)
async def get_job_result(
    job_id: UUID,
    view: str = Query(default="dify"),
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> Response:
    if view != "dify":
        raise HTTPException(status_code=400, detail="Only view=dify is available")

    tenant_id, conversation_hash = owner
    job = await _repo().get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] == "succeeded":
        payload = job.get("compact_result") or {
            "job_id": str(job_id),
            "status": "failed",
            "error_code": "COMPACT_RESULT_MISSING",
        }
        raw = json_bytes(payload)
        if len(raw) > settings.relay_result_hard_limit_bytes:
            # Last defensive barrier: Dify must never receive an oversized result.
            safe = dict(payload)
            safe["text"] = truncate_utf8(
                str(safe.get("text") or ""), settings.relay_result_preview_bytes
            )
            safe["text_truncated"] = True
            raw = json_bytes(safe)
        return Response(content=raw, media_type="application/json", status_code=200)

    if job["status"] in {"failed", "cancelled", "expired"}:
        payload = {
            "job_id": str(job_id),
            "status": job["status"],
            "error_code": job.get("error_code"),
            "error_message": job.get("error_message"),
            "raw_error_stored": bool(job.get("raw_response_object_path")),
        }
        return Response(content=json_bytes(payload), media_type="application/json", status_code=200)

    payload = {
        "job_id": str(job_id),
        "status": job["status"],
        "stage": job["stage"],
        "poll_after_seconds": 10,
    }
    return Response(content=json_bytes(payload), media_type="application/json", status_code=202)


@app.post(
    "/v1/jobs/{job_id}/cancel",
    response_model=CancelResponse,
    dependencies=[Depends(require_relay_auth)],
)
async def cancel_job(
    job_id: UUID,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> CancelResponse:
    tenant_id, conversation_hash = owner
    repository = _repo()
    job = await repository.get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] not in {"succeeded", "failed", "cancelled", "expired"}:
        updated = await repository.update_job(
            job_id,
            {
                "status": "cancelled",
                "completed_at": utcnow().isoformat(),
                "error_code": "CANCELLED_BY_CLIENT",
                "error_message": "Job cancelled by client",
            },
        )
        if updated:
            job = updated

    return CancelResponse(job_id=job_id, status=job["status"])
