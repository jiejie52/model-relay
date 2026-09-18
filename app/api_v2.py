from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse

from .config import Settings
from .errors.service import RawErrorService
from .materials.service import MaterialIngressError, MaterialService
from .models_v2 import MaterialCreateJSON, V2JobSubmitRequest, V2SessionCreateRequest, extract_material_ids
from .providers.registry import ProviderRegistry
from .repository import RelayRepository
from .security import require_relay_auth, require_v2_owner
from .storage.execution_archive import ExecutionArchiveStore
from .storage.uploaded_files import UploadedFileStore
from .storage_paths import v2_job_request_path, v2_session_context_path
from .utils import utcnow
from .v2_utils import canonical_hash, request_fingerprint, scoped_job_idempotency_key

router = APIRouter(prefix="/v2", tags=["relay-v2"], dependencies=[Depends(require_relay_auth)])


def _request_id() -> str:
    return f"req_{uuid4().hex}"


def _envelope(data: Any = None, error: Any = None, *, request_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "relay-envelope/2.0",
        "request_id": request_id or _request_id(),
        "data": data,
        "error": error,
    }


def _json_response(status_code: int, *, data: Any = None, error: Any = None, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=_envelope(data, error, request_id=request_id))


def _services(request: Request) -> tuple[RelayRepository, ExecutionArchiveStore, ProviderRegistry, RawErrorService, UploadedFileStore, Settings]:
    try:
        return (
            request.app.state.repo,
            request.app.state.archive,
            request.app.state.providers,
            request.app.state.errors,
            request.app.state.material_store,
            request.app.state.settings,
        )
    except AttributeError as exc:
        raise HTTPException(status_code=503, detail="Relay V2 services are not initialized") from exc


def _material_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "material_id": row.get("id"),
        "filename": row.get("filename"),
        "declared_mime": row.get("declared_mime"),
        "detected_mime": row.get("detected_mime"),
        "byte_length": row.get("byte_length"),
        "sha256": row.get("sha256"),
        "status": row.get("status"),
        "phase": row.get("phase"),
        "generation": row.get("generation"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "last_error_id": row.get("last_error_id"),
    }


def _session_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": row.get("id"),
        "provider": row.get("provider"),
        "upstream_profile": row.get("upstream_profile"),
        "account_scope": row.get("account_scope"),
        "protocol": row.get("protocol"),
        "history_codec": row.get("history_codec"),
        "history_codec_version": row.get("history_codec_version"),
        "history_mode": row.get("history_mode"),
        "model": row.get("model"),
        "history_version": row.get("history_version"),
        "context_hash": row.get("context_hash"),
        "material_set_hash": row.get("material_set_hash"),
        "active_job_id": row.get("active_job_id"),
        "capability_profile_version": row.get("capability_profile_version"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
    }


def _job_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": row.get("relay_session_id"),
        "job_id": row.get("id"),
        "status": row.get("status"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "execution_phase": row.get("execution_phase"),
        "delivery_status": row.get("delivery_status"),
        "history_version": row.get("expected_history_version"),
        "attempt_count": row.get("attempt_count"),
        "heartbeat_at": row.get("heartbeat_at"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "error_id": row.get("error_id"),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
    }


@router.get("/capabilities")
async def capabilities(request: Request) -> JSONResponse:
    _, _, providers, _, _, _ = _services(request)
    return _json_response(200, data={"profiles": providers.capabilities()})


@router.post("/materials")
async def create_material(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, archive, _, errors, store, settings = _services(request)
    if store is None:
        return _json_response(503, error={"origin": "dependency", "service": "railway_storage", "code": "MATERIAL_STORAGE_NOT_CONFIGURED", "message": "MATERIAL_S3_* configuration is required"})
    service = MaterialService(repo, store, errors, settings)
    content_type = (request.headers.get("content-type") or "").lower()

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            return _json_response(400, error={"origin": "relay", "code": "MATERIAL_FILE_REQUIRED", "message": "multipart field 'file' is required"})
        filename = str(form.get("filename") or upload.filename or "upload.bin")
        declared_mime = str(form.get("declared_mime") or upload.content_type or "") or None
        expected_sha256 = str(form.get("expected_sha256") or "").strip() or None
        expected_size_raw = str(form.get("expected_size") or "").strip()
        expected_size = int(expected_size_raw) if expected_size_raw else None
        reserved = await service.reserve(
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            idempotency_key=idempotency_key,
            filename=filename,
            declared_mime=declared_mime,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            expires_at=None,
            source_identity={"kind": "multipart", "filename": filename},
        )
        material = reserved.get("material") or {}
        if reserved.get("outcome") == "reused":
            code = 201 if material.get("status") == "ready" else 202
            return _json_response(code, data=_material_public(material))
        ingestion_id = str(reserved.get("ingestion_id") or "")
        try:
            ready = await service.ingest_fileobj(
                material=material,
                ingestion_id=ingestion_id,
                fileobj=upload.file,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
            )
        except MaterialIngressError as exc:
            await repo.fail_material_ingestion_v2(
                material_id=str(material.get("id")),
                ingestion_id=ingestion_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                error_id=exc.error_id,
                phase="verifying",
                lease_token=None,
            )
            return _json_response(422, error={"origin": "relay", "code": exc.code, "message": exc.message, "raw_error_id": exc.error_id})
        return _json_response(201, data=_material_public(ready))

    try:
        body = MaterialCreateJSON.model_validate(await request.json())
    except Exception as exc:
        return _json_response(400, error={"origin": "relay", "code": "INVALID_MATERIAL_REQUEST", "message": str(exc)})
    if body.source.expires_at is not None and body.source.expires_at <= utcnow() + timedelta(seconds=settings.material_fetch_timeout_seconds):
        return _json_response(409, error={"origin": "relay", "code": "MATERIAL_SOURCE_EXPIRING", "message": "Source URL expiry window is too short for reliable capture; upload bytes instead"})
    reserved = await service.reserve(
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
        idempotency_key=idempotency_key,
        filename=body.filename,
        declared_mime=body.declared_mime,
        expected_sha256=body.expected_sha256,
        expected_size=body.expected_size,
        expires_at=body.expires_at,
        source_identity=body.source.model_dump(mode="json"),
    )
    material = reserved.get("material") or {}
    if reserved.get("outcome") == "created":
        # Durable URL fetching is done by app.material_worker. The API has only
        # accepted the source identity; ready is not reported prematurely.
        await repo.backend.update(
            "relay_materials",
            {"phase": "awaiting_fetch", "updated_at": utcnow().isoformat()},
            filters={"id": f"eq.{material['id']}"},
        )
        if reserved.get("ingestion_id"):
            await repo.backend.update(
                "relay_material_ingestions",
                {"phase": "awaiting_fetch", "updated_at": utcnow().isoformat()},
                filters={"id": f"eq.{reserved['ingestion_id']}"},
            )
        material["phase"] = "awaiting_fetch"
        return _json_response(202, data=_material_public(material))
    return _json_response(201 if material.get("status") == "ready" else 202, data=_material_public(material))


@router.get("/materials/{material_id}")
async def get_material(
    material_id: str,
    request: Request,
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, *_ = _services(request)
    material = await repo.get_material(material_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    if not material:
        return _json_response(404, error={"origin": "relay", "code": "MATERIAL_NOT_FOUND", "message": "Material not found"})
    return _json_response(200, data=_material_public(material))


@router.post("/materials/{material_id}/retry")
async def retry_material(
    material_id: str,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, *_ = _services(request)
    ingestion_id = f"ing_{uuid4().hex}"
    result = await repo.retry_material_v2(
        material_id=material_id,
        ingestion_id=ingestion_id,
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint({"material_id": material_id}),
    )
    outcome = result.get("outcome")
    if outcome == "not_found":
        return _json_response(404, error={"origin": "relay", "code": "MATERIAL_NOT_FOUND", "message": "Material not found"})
    if outcome == "upload_required":
        return _json_response(409, error={"origin": "relay", "code": "MATERIAL_REUPLOAD_REQUIRED", "message": "This material originated from multipart bytes; retry requires a new explicit upload"})
    if outcome in {"conflict", "state_conflict"}:
        return _json_response(409, error={"origin": "relay", "code": "MATERIAL_RETRY_CONFLICT", "message": "Material retry conflicts with current state or idempotency identity"})
    material = result.get("material") or {}
    return _json_response(200 if outcome == "already_ready" else 202, data=_material_public(material))


@router.delete("/materials/{material_id}")
async def delete_material(
    material_id: str,
    request: Request,
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, _, _, _, store, _ = _services(request)
    if store is None:
        return _json_response(503, error={"origin": "dependency", "service": "railway_storage", "code": "MATERIAL_STORAGE_NOT_CONFIGURED", "message": "MATERIAL_S3_* configuration is required"})
    result = await repo.request_material_delete_v2(
        material_id=material_id,
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
    )
    outcome = result.get("outcome")
    if outcome == "not_found":
        return _json_response(404, error={"origin": "relay", "code": "MATERIAL_NOT_FOUND", "message": "Material not found"})
    if outcome == "referenced":
        return _json_response(409, error={"origin": "relay", "code": "MATERIAL_IN_USE", "message": "Material still has active Session references", "reference_count": result.get("reference_count")})
    material = result.get("material") or {}
    if outcome == "deleting" and material.get("object_key"):
        try:
            await store.delete(material["object_key"])
            rows = await repo.backend.update(
                "relay_materials",
                {"status": "deleted", "phase": "deleted", "deleted_at": utcnow().isoformat(), "updated_at": utcnow().isoformat()},
                filters={"id": f"eq.{material_id}", "tenant_id": f"eq.{tenant_id}", "conversation_hash": f"eq.{conversation_hash}"},
            )
            if rows:
                material = rows[0]
        except Exception as exc:
            return _json_response(503, error={"origin": "dependency", "service": "railway_storage", "code": "MATERIAL_DELETE_FAILED", "message": str(exc)})
    return _json_response(200, data=_material_public(material))


@router.post("/sessions")
async def create_session(
    body: V2SessionCreateRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, archive, providers, _, _, settings = _services(request)
    try:
        profile = providers.validate_session_model(body.upstream_profile, body.provider, body.defaults.model)
    except (KeyError, ValueError) as exc:
        return _json_response(422, error={"origin": "relay", "code": "PROVIDER_PROFILE_INVALID", "message": str(exc)})

    material_ids = extract_material_ids(body.context)
    materials = await repo.get_materials_for_owner(material_ids, tenant_id=tenant_id, conversation_hash=conversation_hash)
    if len(materials) != len(material_ids) or any(m.get("status") != "ready" for m in materials):
        return _json_response(409, error={"origin": "relay", "code": "MATERIAL_NOT_READY", "message": "Every material_ref must belong to this owner and be ready"})
    for material in materials:
        expires_at = material.get("expires_at")
        if not expires_at:
            continue
        try:
            from datetime import datetime
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except Exception:
            return _json_response(409, error={"origin": "relay", "code": "MATERIAL_EXPIRY_INVALID", "message": "Material expiry metadata is invalid"})
        if expiry <= utcnow():
            return _json_response(409, error={"origin": "relay", "code": "MATERIAL_EXPIRED", "message": "At least one material is expired"})

    session_id = uuid4()
    context_hash = canonical_hash(body.context)
    material_set_hash = canonical_hash(material_ids)
    logical_request = {
        "provider": body.provider,
        "upstream_profile": body.upstream_profile,
        "history_mode": body.history_mode,
        "model": body.defaults.model,
        "context_hash": context_hash,
        "material_set_hash": material_set_hash,
    }
    fingerprint = request_fingerprint(logical_request)
    context_path = v2_session_context_path(settings, tenant_id, conversation_hash, str(session_id))
    await archive.put_json(context_path, body.context, upsert=False)
    expiry = body.expires_at or (utcnow() + timedelta(seconds=settings.session_ttl_seconds))
    if materials:
        material_expiries = []
        for item in materials:
            try:
                from datetime import datetime
                material_expiries.append(datetime.fromisoformat(str(item["expires_at"]).replace("Z", "+00:00")))
            except Exception:
                pass
        if material_expiries:
            expiry = min([expiry, *material_expiries])

    result = await repo.create_session_v2(
        session_id=str(session_id),
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        provider=profile.provider,
        upstream_profile=profile.name,
        account_scope=profile.account_scope,
        protocol=profile.protocol,
        history_codec=profile.history_codec,
        history_codec_version=profile.history_codec_version,
        history_mode=body.history_mode,
        model=body.defaults.model,
        context_object_path=context_path,
        context_hash=context_hash,
        material_set_hash=material_set_hash,
        material_ids=material_ids,
        capability_profile_version=profile.capability_profile_version,
        expires_at=expiry.isoformat(),
    )
    outcome = result.get("outcome")
    if outcome == "conflict":
        return _json_response(409, error={"origin": "relay", "code": "IDEMPOTENCY_CONFLICT", "message": "Idempotency-Key was used for a different Session request"})
    if outcome == "material_not_ready":
        return _json_response(409, error={"origin": "relay", "code": "MATERIAL_NOT_READY", "message": "Session material closure is not ready"})
    session = result.get("session") or {}
    return _json_response(201 if outcome == "created" else 200, data=_session_public(session))


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, *_ = _services(request)
    session = await repo.get_session(session_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    if not session or session.get("schema_version") != "relay-session/2.0":
        return _json_response(404, error={"origin": "relay", "code": "SESSION_NOT_FOUND", "message": "Session not found"})
    data = _session_public(session)
    materials = await repo.get_session_materials(str(session_id))
    data["materials"] = [_material_public(m) for m in materials]
    data["material_valid"] = all(m.get("status") == "ready" for m in materials)
    return _json_response(200, data=data)


@router.post("/sessions/{session_id}/jobs")
async def submit_session_job(
    session_id: UUID,
    body: V2JobSubmitRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, archive, providers, _, _, settings = _services(request)
    session = await repo.get_session(session_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    if not session or session.get("schema_version") != "relay-session/2.0":
        return _json_response(404, error={"origin": "relay", "code": "SESSION_NOT_FOUND", "message": "Session not found"})
    model = body.model or str(session.get("model") or "")
    try:
        profile = providers.validate_session_model(str(session["upstream_profile"]), str(session["provider"]), model)
    except (KeyError, ValueError) as exc:
        return _json_response(422, error={"origin": "relay", "code": "MODEL_NOT_ALLOWED", "message": str(exc)})
    if model != str(session.get("model") or "") and not bool(profile.capabilities.get("allow_model_switch")):
        return _json_response(409, error={"origin": "relay", "code": "SESSION_MODEL_HISTORY_INCOMPATIBLE", "message": "This profile does not declare lossless history compatibility for model switching"})

    input_material_ids = extract_material_ids(body.input)
    if input_material_ids:
        bound_material_ids = set(await repo.get_session_material_ids(str(session_id)))
        unbound = [mid for mid in input_material_ids if mid not in bound_material_ids]
        if unbound:
            return _json_response(409, error={
                "origin": "relay",
                "code": "SESSION_MATERIAL_SET_IMMUTABLE",
                "message": "Job input cannot add materials that are not bound to the Session",
                "unbound_material_ids": unbound,
            })

    logical = {
        "session_id": str(session_id),
        "context_hash": session.get("context_hash"),
        "material_set_hash": session.get("material_set_hash"),
        "provider": session.get("provider"),
        "upstream_profile": session.get("upstream_profile"),
        "protocol": session.get("protocol"),
        "model": model,
        "generation": body.generation,
        "structured_output": body.structured_output,
        "expected_history_version": body.expected_history_version,
        "input": body.input,
    }
    fingerprint = request_fingerprint(logical)
    job_id = uuid4()
    snapshot = {
        **logical,
        "structured_output": body.structured_output or {},
        "metadata": body.metadata,
        "capability_profile_version": profile.capability_profile_version,
    }
    request_path = v2_job_request_path(settings, tenant_id, conversation_hash, str(job_id))
    await archive.put_json(request_path, snapshot, upsert=False)
    scope = f"session_job:{session_id}"
    result = await repo.submit_session_job_v2(
        job_id=str(job_id),
        session_id=str(session_id),
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
        idempotency_key=idempotency_key,
        job_idempotency_key=scoped_job_idempotency_key(scope, idempotency_key),
        request_fingerprint=fingerprint,
        request_object_path=request_path,
        provider=str(session["provider"]),
        model=model,
        expected_history_version=body.expected_history_version,
        capability_version=profile.capability_profile_version,
        expires_at=(utcnow() + timedelta(seconds=settings.job_ttl_seconds)).isoformat(),
    )
    outcome = result.get("outcome")
    if outcome == "conflict":
        return _json_response(409, error={"origin": "relay", "code": "IDEMPOTENCY_CONFLICT", "message": "Idempotency-Key was used with a different logical request"})
    if outcome == "history_conflict":
        return _json_response(409, error={"origin": "relay", "code": "SESSION_HISTORY_CONFLICT", "message": "expected_history_version does not match Session", "current_history_version": result.get("history_version")})
    if outcome == "session_busy":
        return _json_response(409, error={"origin": "relay", "code": "SESSION_BUSY", "message": "Another Job is already advancing this Session", "active_job_id": result.get("active_job_id")})
    if outcome not in {"created", "reused"}:
        return _json_response(409, error={"origin": "relay", "code": "SESSION_JOB_REJECTED", "message": f"Session Job submission rejected: {outcome}"})
    job = result.get("job") or {}
    return _json_response(202, data=_job_public(job))


@router.get("/sessions/{session_id}/jobs/{job_id}")
async def get_session_job(
    session_id: UUID,
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, *_ = _services(request)
    job = await repo.get_job(job_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    if not job or str(job.get("relay_session_id") or "") != str(session_id) or job.get("execution_engine") != "v2":
        return _json_response(404, error={"origin": "relay", "code": "JOB_NOT_FOUND", "message": "Job not found in this Session"})
    return _json_response(200, data=_job_public(job))


@router.get("/sessions/{session_id}/jobs/{job_id}/result")
async def get_session_job_result(
    session_id: UUID,
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, archive, _, errors, _, settings = _services(request)
    job = await repo.get_job(job_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    if not job or str(job.get("relay_session_id") or "") != str(session_id) or job.get("execution_engine") != "v2":
        return _json_response(404, error={"origin": "relay", "code": "JOB_NOT_FOUND", "message": "Job not found in this Session"})
    if job.get("status") not in {"succeeded", "failed", "cancelled", "expired"}:
        return _json_response(202, data=_job_public(job))
    if job.get("status") == "succeeded":
        result_obj = None
        if job.get("response_output_object_path"):
            result_obj = await archive.get_json(job["response_output_object_path"])
        data = _job_public(job)
        data["result"] = result_obj
        return _json_response(200, data=data)

    error_view = None
    if job.get("error_id"):
        row = await repo.get_raw_error(str(job["error_id"]), tenant_id=tenant_id, conversation_hash=conversation_hash)
        if row:
            include = settings.raw_error_delivery == "inline" or (
                settings.raw_error_delivery == "auto" and int(row.get("byte_length") or 0) <= settings.raw_error_inline_max_bytes
            )
            error_view = await errors.external_view(row, include_body=include)
            if not include:
                error_view["raw_path"] = f"/v2/errors/{row['id']}/raw"
    data = _job_public(job)
    data["result"] = None
    return _json_response(200, data=data, error=error_view or {"origin": "relay", "code": job.get("error_code"), "message": job.get("error_message")})


@router.post("/sessions/{session_id}/jobs/{job_id}/cancel")
async def cancel_session_job(
    session_id: UUID,
    job_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, *_ = _services(request)
    result = await repo.cancel_job_v2(
        job_id=str(job_id),
        session_id=str(session_id),
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
    )
    if result.get("outcome") == "not_found":
        return _json_response(404, error={"origin": "relay", "code": "JOB_NOT_FOUND", "message": "Job not found in this Session"})
    return _json_response(200, data=_job_public(result.get("job") or {}))


@router.get("/errors/{error_id}")
async def get_error(
    error_id: str,
    request: Request,
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> JSONResponse:
    tenant_id, conversation_hash = owner
    repo, _, _, errors, _, settings = _services(request)
    row = await repo.get_raw_error(error_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    if not row:
        return _json_response(404, error={"origin": "relay", "code": "ERROR_NOT_FOUND", "message": "Raw Error not found"})
    include = settings.raw_error_delivery == "inline" or (
        settings.raw_error_delivery == "auto" and int(row.get("byte_length") or 0) <= settings.raw_error_inline_max_bytes
    )
    data = await errors.external_view(row, include_body=include)
    if not include:
        data["raw_path"] = f"/v2/errors/{error_id}/raw"
    return _json_response(200, data=data)


@router.get("/errors/{error_id}/raw")
async def get_error_raw(
    error_id: str,
    request: Request,
    owner: tuple[str, str] = Depends(require_v2_owner),
) -> Response:
    tenant_id, conversation_hash = owner
    repo, archive, *_ = _services(request)
    row = await repo.get_raw_error(error_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    if not row or not row.get("body_object_path"):
        raise HTTPException(status_code=404, detail="Raw Error body not available")
    raw = await archive.get_bytes(row["body_object_path"])
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{error_id}.bin"',
        "X-Relay-Body-Sha256": str(row.get("sha256") or ""),
    }
    return Response(content=raw, media_type="application/octet-stream", headers=headers)
