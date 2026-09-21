from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import ValidationError

from ..core.idempotency import request_identity, stable_hash
from ..core.raw_error import raw_body_inline_fields
from ..materials.ingress import MaterialIngressError
from ..observability import error as log_error, info as log_info
from ..persistence.object_storage import ObjectLocation
from ..security import require_owner_headers, require_relay_auth
from ..storage_paths import request_object_path_v2
from ..supabase import SupabaseError
from ..utils import json_bytes, stable_prompt_cache_key, utcnow
from ..v2_models import (
    MaterialCreateJSON,
    MaterialResponse,
    RequestEnvelope,
    SessionCreateRequest,
    SessionRequestCreate,
    SessionResponse,
)


logger = logging.getLogger("model-relay-api.v2")

router = APIRouter(prefix="/v2", tags=["relay-v2"], dependencies=[Depends(require_relay_auth)])


def _repo(request: Request):
    return request.app.state.v2_repo


def _settings(request: Request):
    return request.app.state.settings


def _material_ingress(request: Request):
    return request.app.state.material_ingress


def _provider_files(request: Request):
    return request.app.state.provider_file_registry


def _storage(request: Request):
    return request.app.state.storage_registry


def _inline(request: Request):
    return request.app.state.inline_executor


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _session_response(row: dict[str, Any]) -> SessionResponse:
    material_manifest = row.get("material_manifest") or []
    return SessionResponse(
        session_id=UUID(str(row["id"])),
        tenant_id=row["tenant_id"],
        conversation_hash=row["conversation_hash"],
        provider=row["provider"],
        connection_id=row.get("connection_id") or "aihubmix_default",
        model=row["model"],
        context_policy=row.get("context_policy") or "conversation",
        material_ids=[str(x) for x in material_manifest] if isinstance(material_manifest, list) else [],
        history_version=int(row.get("history_version") or 0),
        active_request_id=UUID(row["active_request_id"]) if row.get("active_request_id") else None,
        execution_pool=row.get("execution_pool") or "railway-default",
        created_at=_dt(row.get("created_at")),
        expires_at=_dt(row.get("expires_at")),
    )


def _request_envelope(row: dict[str, Any]) -> RequestEnvelope:
    error = row.get("error") if isinstance(row.get("error"), dict) else None
    execution_mode = str(row.get("execution_mode") or "async")
    return RequestEnvelope(
        session_id=UUID(str(row["session_id"])),
        request_id=UUID(str(row["id"])),
        job_id=UUID(str(row["job_id"])) if row.get("job_id") else None,
        execution={"mode": execution_mode},
        status=str(row["status"]),
        history_version=int(row.get("completed_history_version") or row.get("expected_history_version") or 0),
        result=row.get("compact_result") if isinstance(row.get("compact_result"), dict) else None,
        error=error,
        poll_after_seconds=5 if row.get("status") in {"queued", "leased", "running"} else None,
    )


async def _request_envelope_hydrated(request: Request, row: dict[str, Any]) -> RequestEnvelope:
    """Hydrate archived 0.3.0 errors so idempotent replay also returns raw body.

    New 0.3.1 failures already persist body_text/body_base64 in relay_requests.error.
    For an older failed Request that only has body_object_id, load the archived
    bytes and enrich the response in-memory without requiring a DB migration.
    """
    error = row.get("error") if isinstance(row.get("error"), dict) else None
    if error and error.get("body_object_id") and not error.get("body_base64"):
        obj = await _repo(request).get_object(
            error["body_object_id"],
            tenant_id=row.get("tenant_id"),
            conversation_hash=row.get("conversation_hash"),
        )
        if obj:
            data = await _storage(request).get(obj["storage_id"]).get_bytes(
                ObjectLocation(obj["storage_id"], obj["bucket"], obj["object_key"])
            )
            enriched = dict(error)
            enriched.update(
                raw_body_inline_fields(
                    data,
                    content_type=str(error.get("content_type") or obj.get("content_type") or "application/octet-stream"),
                    content_encoding=error.get("content_encoding"),
                )
            )
            if enriched.get("body_text") is not None and enriched.get("upstream_http_status") is not None:
                enriched["message"] = enriched["body_text"]
            row = dict(row)
            row["error"] = enriched
    return _request_envelope(row)


async def _material_response(request: Request, row: dict[str, Any]) -> MaterialResponse:
    repo = _repo(request)
    fallback = await repo.get_material_fallback(row["id"])
    target_connection_id = row.get("target_connection_id")
    binding = None
    if target_connection_id:
        adapter = _provider_files(request).maybe_get(str(target_connection_id))
        account_scope_hash = adapter.account_scope_hash if adapter is not None else None
        binding = await repo.get_provider_binding(
            material_id=row["id"],
            connection_id=str(target_connection_id),
            account_scope_hash=account_scope_hash,
        )
    status_value = str(row.get("status") or "")
    external_status = "ready" if status_value in {"ready", "ready_provider"} else status_value
    ready_for: list[str] = []
    if binding and str(binding.get("state") or binding.get("processing_state") or "").lower() in {"active", "ready", "processed"}:
        ready_for.append(str(binding["connection_id"]))
    elif status_value == "ready" and target_connection_id and fallback:
        # Bridge-only providers (e.g. signed-URL transport) are ready when the
        # frozen fallback object exists even without a provider file resource.
        ready_for.append(str(target_connection_id))
    object_id = fallback.get("object_id") if fallback else row.get("object_id")
    storage_id = fallback.get("storage_id") if fallback else None
    provider_binding = None
    if binding:
        provider_binding = {
            "provider": binding.get("provider"),
            "connection_id": binding["connection_id"],
            "state": str(binding.get("state") or binding.get("processing_state") or "unknown"),
            "generation": int(binding.get("generation") or 1),
            "purpose": binding.get("purpose"),
            "representation": binding.get("representation"),
            "expires_at": _dt(binding.get("expires_at")),
        }
    size_bytes = int(row.get("actual_size") or row.get("size_bytes") or 0)
    return MaterialResponse(
        material_id=row["id"],
        status=external_status,
        filename=row["filename"],
        content_type=row["content_type"],
        size=size_bytes,
        size_bytes=size_bytes,
        sha256=row["sha256"],
        durability=str(row.get("durability") or ("relay_backed" if fallback else "provider_bound")),
        ready_for=ready_for,
        fallback={
            "stored": bool(fallback),
            "object_ref": f"internal://objects/{object_id}" if object_id else None,
            "storage_id": storage_id,
        },
        provider_binding=provider_binding,
        object_id=object_id,
        storage_id=storage_id,
        source_ref=row.get("source_ref"),
        parent_material_id=row.get("parent_material_id"),
        ordinal=row.get("ordinal"),
        created_at=_dt(row.get("created_at")),
    )


@router.post("/materials", response_model=MaterialResponse, status_code=status.HTTP_201_CREATED)
async def create_material(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
) -> MaterialResponse:
    content_type_header = str(request.headers.get("content-type") or "").lower()
    data: bytes | None = None
    try:
        if content_type_header.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(status_code=400, detail="multipart material requires file field")
            chunks: list[bytes] = []
            total = 0
            max_bytes = _settings(request).material_ingress_max_bytes
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail="material exceeds configured ingress size limit")
                chunks.append(chunk)
            data = b"".join(chunks)
            tenant_id = str(form.get("tenant_id") or "")
            conversation_hash = str(form.get("conversation_hash") or "")
            filename = str(form.get("filename") or getattr(upload, "filename", "material.bin"))
            content_type = str(form.get("content_type") or getattr(upload, "content_type", "") or "application/octet-stream")
            source_ref = str(form.get("source_ref") or "") or None
            parent_material_id = str(form.get("parent_material_id") or "") or None
            ordinal = int(form["ordinal"]) if form.get("ordinal") not in (None, "") else None
            metadata = json.loads(str(form.get("metadata") or "{}"))
            target_connection_id = str(form.get("target_connection_id") or "") or None
            durability_policy = str(form.get("durability_policy") or _settings(request).material_default_durability_policy)
            fallback_policy = str(form.get("fallback_policy") or _settings(request).material_default_fallback_policy)
            declared_size = int(form["declared_size"]) if form.get("declared_size") not in (None, "") else None
            source_url = None
        else:
            payload = MaterialCreateJSON.model_validate(await request.json())
            tenant_id = payload.tenant_id
            conversation_hash = payload.conversation_hash
            filename = payload.filename
            content_type = payload.content_type
            source_url = payload.source_url
            source_ref = payload.source_ref
            parent_material_id = payload.parent_material_id
            ordinal = payload.ordinal
            metadata = payload.metadata
            target_connection_id = payload.target_connection_id
            durability_policy = payload.durability_policy
            fallback_policy = payload.fallback_policy
            declared_size = payload.declared_size
            if payload.content_base64:
                data = base64.b64decode(payload.content_base64, validate=True)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not tenant_id or not conversation_hash:
        raise HTTPException(status_code=400, detail="tenant_id and conversation_hash are required")
    if target_connection_id and target_connection_id not in _settings(request).enabled_connection_set:
        raise HTTPException(status_code=409, detail=f"Connection is not enabled here: {target_connection_id}")
    try:
        row = await _material_ingress(request).create(
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            idempotency_key=idempotency_key,
            filename=filename,
            content_type=content_type,
            data=data,
            source_url=source_url,
            source_ref=source_ref,
            parent_material_id=parent_material_id,
            ordinal=ordinal,
            metadata=metadata,
            target_connection_id=target_connection_id,
            durability_policy=durability_policy,
            fallback_policy=fallback_policy,
            declared_size=declared_size,
        )
    except MaterialIngressError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    except Exception as exc:
        log_error(
            logger,
            "material_api_failed",
            exc_info=True,
            target_connection_id=target_connection_id if "target_connection_id" in locals() else None,
            filename=filename if "filename" in locals() else None,
            failure_class="relay",
            exception_type=type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log_info(
        logger,
        "material_api_accepted",
        material_id=row.get("id"),
        target_connection_id=row.get("target_connection_id"),
        status=row.get("status"),
        durability=row.get("durability"),
        size_bytes=row.get("actual_size") or row.get("size_bytes"),
        content_type=row.get("content_type"),
    )
    return await _material_response(request, row)


@router.get("/materials/{material_id}", response_model=MaterialResponse)
async def get_material(
    material_id: str,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> MaterialResponse:
    tenant_id, conversation_hash = owner
    row = await _repo(request).get_material(
        material_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not row:
        raise HTTPException(status_code=404, detail="Material not found")
    return await _material_response(request, row)


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreateRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
) -> SessionResponse:
    repo = _repo(request)
    existing = await repo.find_session_by_idempotency(body.tenant_id, idempotency_key)
    session_hash = stable_hash(body.model_dump(mode="json"))
    if existing:
        if existing.get("session_hash") != session_hash:
            raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different Session")
        log_info(
            logger,
            "session_idempotent_reuse",
            session_id=existing.get("id"),
            provider=existing.get("provider"),
            connection_id=existing.get("connection_id"),
            model=existing.get("model"),
            execution_pool=existing.get("execution_pool"),
        )
        return _session_response(existing)

    for material_id in body.material_ids:
        material = await repo.get_material(
            material_id,
            tenant_id=body.tenant_id,
            conversation_hash=body.conversation_hash,
        )
        if not material or material.get("status") in {"failed", "deleted", "reupload_required", "receiving", "binding"}:
            raise HTTPException(status_code=409, detail=f"Material is not usable: {material_id}")

    settings = _settings(request)
    if body.connection_id not in settings.enabled_connection_set:
        raise HTTPException(status_code=409, detail=f"Connection is not enabled here: {body.connection_id}")
    if body.execution_pool and body.execution_pool != settings.execution_pool:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXECUTION_POOL_MISMATCH",
                "requested_pool": body.execution_pool,
                "local_pool": settings.execution_pool,
                "deployment_id": settings.deployment_id,
            },
        )
    session_id = uuid4()
    row = await repo.create_session(
        {
            "id": str(session_id),
            "tenant_id": body.tenant_id,
            "conversation_hash": body.conversation_hash,
            "provider": body.provider,
            "connection_id": body.connection_id,
            "model": body.model,
            "prompt_cache_key": stable_prompt_cache_key(body.conversation_hash),
            "material_prefix_object_path": None,
            "history_object_path": None,
            "history_object_id": None,
            "signed_url_expires_at": None,
            "history_version": 0,
            "context_policy": body.context_policy,
            "material_manifest": body.material_ids,
            "active_request_id": None,
            "execution_pool": body.execution_pool or settings.execution_pool,
            "protocol_version": "v2",
            "idempotency_key": idempotency_key,
            "session_hash": session_hash,
            "metadata": body.metadata,
            "expires_at": repo.default_session_expiry().isoformat(),
        }
    )
    log_info(
        logger,
        "session_created",
        session_id=row.get("id"),
        provider=row.get("provider"),
        connection_id=row.get("connection_id"),
        model=row.get("model"),
        context_policy=row.get("context_policy"),
        execution_pool=row.get("execution_pool"),
        material_count=len(row.get("material_manifest") or []),
    )
    return _session_response(row)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> SessionResponse:
    tenant_id, conversation_hash = owner
    row = await _repo(request).get_session(
        session_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_response(row)


@router.post("/sessions/{session_id}/requests", response_model=RequestEnvelope)
async def create_request(
    session_id: UUID,
    body: SessionRequestCreate,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> RequestEnvelope:
    tenant_id, conversation_hash = owner
    repo = _repo(request)
    session = await repo.get_session(
        session_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    settings = _settings(request)
    if str(session.get("execution_pool") or "") != settings.execution_pool:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SESSION_EXECUTION_POOL_MISMATCH",
                "session_execution_pool": session.get("execution_pool"),
                "local_execution_pool": settings.execution_pool,
                "deployment_id": settings.deployment_id,
            },
        )

    provider = body.provider or session["provider"]
    connection_id = body.connection_id or session.get("connection_id")
    model = body.model or session["model"]
    if provider != session["provider"] or connection_id != session.get("connection_id") or model != session["model"]:
        raise HTTPException(
            status_code=409,
            detail="Session provider/connection/model is immutable; create or derive another Session",
        )
    effective_material_ids = list(body.material_ids)
    if session.get("context_policy") == "conversation" and isinstance(session.get("material_manifest"), list):
        effective_material_ids = [str(x) for x in session.get("material_manifest") or []] + effective_material_ids
    effective_material_ids = list(dict.fromkeys(effective_material_ids))
    material_hashes: dict[str, str] = {}
    for material_id in effective_material_ids:
        material = await repo.get_material(
            material_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        if not material or material.get("status") in {"failed", "deleted", "reupload_required", "receiving", "binding"}:
            raise HTTPException(status_code=409, detail=f"Material is not usable: {material_id}")
        material_hashes[material_id] = str(material.get("sha256") or "")

    snapshot = {
        "schema_version": "relay-request/2.0",
        "session_id": str(session_id),
        "tenant_id": tenant_id,
        "conversation_hash": conversation_hash,
        "input": body.input,
        "instructions": body.instructions,
        "material_ids": body.material_ids,
        "material_hashes": material_hashes,
        "provider": provider,
        "connection_id": connection_id,
        "model": model,
        "think_level": body.think_level,
        "execution": body.execution.model_dump(mode="json"),
        "structured_output": body.structured_output,
        "provider_payload": body.provider_payload,
        "metadata": body.metadata,
    }
    req_hash = request_identity(snapshot)
    existing = await repo.find_request_by_idempotency(session_id, idempotency_key)
    if existing:
        if existing.get("request_hash") != req_hash:
            raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different Request")
        log_info(
            logger,
            "request_idempotent_reuse",
            request_id=existing.get("id"),
            session_id=existing.get("session_id"),
            job_id=existing.get("job_id"),
            execution_mode=existing.get("execution_mode"),
            status=existing.get("status"),
            connection_id=existing.get("connection_id"),
            model=existing.get("model"),
        )
        return await _request_envelope_hydrated(request, existing)

    request_id = uuid4()
    object_id = f"obj_{uuid4().hex}"
    raw = json_bytes(snapshot)
    digest = hashlib.sha256(raw).hexdigest()
    path = request_object_path_v2(
        _settings(request), tenant_id, conversation_hash, str(session_id), str(request_id), "request.json"
    )
    backend = _storage(request).get(_settings(request).default_storage_id)
    location = await backend.put_bytes(path, raw, content_type="application/json")
    await repo.create_object(
        {
            "id": object_id,
            "tenant_id": tenant_id,
            "conversation_hash": conversation_hash,
            "storage_id": location.storage_id,
            "bucket": location.bucket,
            "object_key": location.key,
            "sha256": digest,
            "size_bytes": len(raw),
            "content_type": "application/json",
            "created_at": utcnow().isoformat(),
        }
    )

    job_id = str(uuid4()) if body.execution.mode == "async" else None
    try:
        row = await repo.accept_request(
            session_id=str(session_id),
            request_id=str(request_id),
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            idempotency_key=idempotency_key,
            request_hash=req_hash,
            execution_mode=body.execution.mode,
            request_object_id=object_id,
            provider=provider,
            connection_id=str(connection_id),
            model=model,
            execution_pool=session.get("execution_pool") or _settings(request).execution_pool,
            metadata=body.metadata,
            job_id=job_id,
        )
    except SupabaseError as exc:
        text = exc.body
        if "SESSION_BUSY" in text:
            raise HTTPException(status_code=409, detail="SESSION_BUSY") from exc
        if "IDEMPOTENCY_CONFLICT" in text:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT") from exc
        log_error(
            logger,
            "request_accept_failed",
            exc_info=True,
            request_id=str(request_id),
            session_id=str(session_id),
            job_id=job_id,
            execution_mode=body.execution.mode,
            provider=provider,
            connection_id=connection_id,
            model=model,
            failure_class="dependency",
            dependency="supabase",
            http_status=exc.status_code,
            exception_type=type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to accept Relay Request") from exc

    log_info(
        logger,
        "request_accepted",
        request_id=row.get("id"),
        session_id=row.get("session_id"),
        job_id=row.get("job_id"),
        execution_mode=row.get("execution_mode"),
        status=row.get("status"),
        provider=provider,
        connection_id=connection_id,
        model=model,
        execution_pool=row.get("execution_pool"),
        material_count=len(effective_material_ids),
        business_stage=(body.metadata or {}).get("stage") or (body.metadata or {}).get("purpose") or (body.input.get("stage") if isinstance(body.input, dict) else None),
    )

    if body.execution.mode == "sync":
        row = await _inline(request).execute(row)
        if not row:
            raise HTTPException(status_code=500, detail="Request disappeared after inline execution")
    return await _request_envelope_hydrated(request, row)


@router.get("/sessions/{session_id}/requests/{request_id}", response_model=RequestEnvelope)
async def get_request(
    session_id: UUID,
    request_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> RequestEnvelope:
    tenant_id, conversation_hash = owner
    await _repo(request).reconcile_request(request_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    row = await _repo(request).get_request(
        request_id,
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
        session_id=session_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    return await _request_envelope_hydrated(request, row)


@router.get("/sessions/{session_id}/requests/{request_id}/result")
async def get_request_result(
    session_id: UUID,
    request_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> Response:
    tenant_id, conversation_hash = owner
    await _repo(request).reconcile_request(request_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    row = await _repo(request).get_request(
        request_id,
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
        session_id=session_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    env = (await _request_envelope_hydrated(request, row)).model_dump(mode="json")
    http_status = 202 if row["status"] in {"queued", "leased", "running"} else 200
    return Response(content=json_bytes(env), media_type="application/json", status_code=http_status)


@router.get("/sessions/{session_id}/requests/{request_id}/error/raw")
async def get_raw_error(
    session_id: UUID,
    request_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> Response:
    tenant_id, conversation_hash = owner
    await _repo(request).reconcile_request(request_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    row = await _repo(request).get_request(
        request_id,
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
        session_id=session_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    error = row.get("error") if isinstance(row.get("error"), dict) else None
    if not error or not error.get("body_object_id"):
        raise HTTPException(status_code=404, detail="No archived raw error body is available")
    obj = await _repo(request).get_object(
        error["body_object_id"], tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not obj:
        raise HTTPException(status_code=404, detail="Raw error object is missing")
    data = await _storage(request).get(obj["storage_id"]).get_bytes(
        ObjectLocation(obj["storage_id"], obj["bucket"], obj["object_key"])
    )
    headers: dict[str, str] = {
        "X-Relay-Error-SHA256": str(error.get("body_sha256") or ""),
        "X-Relay-Error-Source": str(error.get("source") or ""),
    }
    if error.get("upstream_http_status") is not None:
        headers["X-Upstream-HTTP-Status"] = str(error["upstream_http_status"])
    if error.get("upstream_request_id"):
        headers["X-Upstream-Request-Id"] = str(error["upstream_request_id"])
    if error.get("content_encoding"):
        headers["X-Upstream-Content-Encoding"] = str(error["content_encoding"])
    return Response(
        content=data,
        media_type=str(error.get("content_type") or obj.get("content_type") or "application/octet-stream"),
        headers=headers,
    )


@router.post("/sessions/{session_id}/requests/{request_id}/cancel", response_model=RequestEnvelope)
async def cancel_request(
    session_id: UUID,
    request_id: UUID,
    request: Request,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> RequestEnvelope:
    tenant_id, conversation_hash = owner
    repo = _repo(request)
    row = await repo.get_request(
        request_id,
        tenant_id=tenant_id,
        conversation_hash=conversation_hash,
        session_id=session_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    if row["status"] not in {"succeeded", "failed", "cancelled"}:
        await repo.cancel_request(str(request_id), str(session_id))
        row = await repo.get_request(request_id)
    return await _request_envelope_hydrated(request, row)
