from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from .core_models import SessionCreateRequest, SessionJobRequest
from .core_service import CoreError, RelayCoreService
from .security import require_owner_headers, require_relay_auth
from .utils import json_bytes


router = APIRouter(prefix="/v2", tags=["relay-core-v2"])


def _service(request: Request) -> RelayCoreService:
    return request.app.state.core_service


def _parse_dt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except Exception:
        return value


def _request_id(value: str | None) -> str:
    return value.strip() if value and value.strip() else f"req_{uuid4().hex}"


def _session_obj(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "provider": row.get("provider"),
        "upstream_profile": row.get("upstream_profile_id") or "legacy",
        "protocol": row.get("protocol") or "legacy",
        "history_codec": row.get("history_codec") or "legacy",
        "history_mode": row.get("history_mode") or "append",
        "history_version": int(row.get("history_version") or 0),
        "model": row.get("model") or None,
        "created_at": _parse_dt(row.get("created_at")),
        "expires_at": _parse_dt(row.get("expires_at")),
    }


def _job_obj(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row.get("status"),
        "model": row.get("model"),
        "label": row.get("stage") if row.get("stage") != "session_job" else None,
        "heartbeat_at": _parse_dt(row.get("heartbeat_at")),
        "started_at": _parse_dt(row.get("started_at")),
        "completed_at": _parse_dt(row.get("completed_at")),
        "poll_after_seconds": 5 if row.get("status") in {"queued", "leased", "running", "prepared"} else 0,
    }


def _envelope(
    *,
    request_id: str,
    session: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "relay-envelope/2.0",
        "request_id": request_id,
        "session": _session_obj(session) if session else None,
        "job": _job_obj(job) if job else None,
        "result": result,
        "error": error,
    }


def _raise_core(exc: CoreError) -> None:
    # The application-level CoreError handler serializes this as relay-envelope/2.0.
    raise exc


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_relay_auth)],
)
async def create_session(
    body: SessionCreateRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    service = _service(request)
    try:
        session = await service.create_session(body, idempotency_key=idempotency_key)
    except CoreError as exc:
        _raise_core(exc)
    return _envelope(request_id=_request_id(x_request_id), session=session)


@router.get(
    "/sessions/{session_id}",
    dependencies=[Depends(require_relay_auth)],
)
async def get_session(
    session_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    tenant_id, conversation_hash = owner
    try:
        session = await _service(request).get_session(
            session_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
    except CoreError as exc:
        _raise_core(exc)
    return _envelope(request_id=_request_id(x_request_id), session=session)


@router.post(
    "/sessions/{session_id}/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_relay_auth)],
)
async def submit_session_job(
    session_id: UUID,
    body: SessionJobRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
    owner: tuple[str, str] = Depends(require_owner_headers),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    tenant_id, conversation_hash = owner
    service = _service(request)
    try:
        session = await service.get_session(
            session_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        job = await service.submit_session_job(
            session_id,
            body,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            idempotency_key=idempotency_key,
        )
    except CoreError as exc:
        _raise_core(exc)
    return _envelope(
        request_id=_request_id(x_request_id), session=session, job=job
    )


@router.get(
    "/sessions/{session_id}/jobs/{job_id}",
    dependencies=[Depends(require_relay_auth)],
)
async def get_job(
    session_id: UUID,
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    tenant_id, conversation_hash = owner
    service = _service(request)
    try:
        session = await service.get_session(
            session_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        job = await service.get_job(
            session_id,
            job_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        error = await service.error_view(job) if job.get("status") == "failed" else None
    except CoreError as exc:
        _raise_core(exc)
    return _envelope(
        request_id=_request_id(x_request_id), session=session, job=job, error=error
    )


@router.get(
    "/sessions/{session_id}/jobs/{job_id}/result",
    dependencies=[Depends(require_relay_auth)],
)
async def get_job_result(
    session_id: UUID,
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    tenant_id, conversation_hash = owner
    service = _service(request)
    try:
        session = await service.get_session(
            session_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        job = await service.get_job(
            session_id,
            job_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
    except CoreError as exc:
        _raise_core(exc)

    req_id = _request_id(x_request_id)
    if job.get("status") == "succeeded":
        return _envelope(
            request_id=req_id,
            session=session,
            job=job,
            result=job.get("compact_result") or {},
        )
    if job.get("status") in {"failed", "cancelled", "expired"}:
        error = await service.error_view(job)
        return _envelope(
            request_id=req_id,
            session=session,
            job=job,
            error=error,
        )
    return Response(
        content=json_bytes(_envelope(request_id=req_id, session=session, job=job)),
        media_type="application/json",
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.get(
    "/sessions/{session_id}/jobs/{job_id}/error/raw",
    dependencies=[Depends(require_relay_auth)],
)
async def get_raw_error(
    session_id: UUID,
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
):
    tenant_id, conversation_hash = owner
    service = _service(request)
    try:
        job = await service.get_job(
            session_id,
            job_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
    except CoreError as exc:
        _raise_core(exc)
    raw = await service.raw_error_bytes(job)
    if raw is None:
        raise HTTPException(status_code=404, detail="Raw provider error is not available")
    meta = job.get("raw_error_meta") if isinstance(job.get("raw_error_meta"), dict) else {}
    content_type = "application/octet-stream"
    # Preserve Content-Type as data only when it is a single safe media type.
    for pair in meta.get("response_headers") or []:
        if isinstance(pair, list) and len(pair) == 2 and str(pair[0]).lower() == "content-type":
            content_type = str(pair[1]).split(";", 1)[0] or content_type
            break
    return Response(content=raw, media_type=content_type, status_code=200)


@router.post(
    "/sessions/{session_id}/jobs/{job_id}/cancel",
    dependencies=[Depends(require_relay_auth)],
)
async def cancel_job(
    session_id: UUID,
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    tenant_id, conversation_hash = owner
    service = _service(request)
    try:
        session = await service.get_session(
            session_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        job = await service.cancel_job(
            session_id,
            job_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        error = await service.error_view(job)
    except CoreError as exc:
        _raise_core(exc)
    return _envelope(
        request_id=_request_id(x_request_id), session=session, job=job, error=error
    )
