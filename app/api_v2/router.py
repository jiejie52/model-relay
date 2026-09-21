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
from ..observability import error as log_error, info as log_info, warning as log_warning, elapsed_ms, now_ms
from ..persistence.object_storage import ObjectLocation
from ..routing import RouteBinding, RouteResolutionError
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


def _route_resolver(request: Request):
    return request.app.state.route_resolver


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


def _validation_error_summary(exc: ValidationError) -> list[dict[str, Any]]:
    """Return Pydantic diagnostics without echoing caller payloads into logs.

    ``ValidationError.errors()`` includes an ``input`` field by default. For
    Material API failures that input can be a large base64 file payload or a
    temporary URL carrying credentials in its query string. Keep only the
    structural location/type/message required for operations.
    """
    summary: list[dict[str, Any]] = []
    for item in exc.errors(include_url=False):
        summary.append(
            {
                "loc": [str(part) for part in item.get("loc", ())],
                "type": item.get("type"),
                "msg": item.get("msg"),
            }
        )
    return summary


def _session_response(row: dict[str, Any]) -> SessionResponse:
    material_manifest = row.get("material_manifest") or []
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    route = metadata.get("_relay_route") if isinstance(metadata.get("_relay_route"), dict) else {}
    return SessionResponse(
        session_id=UUID(str(row["id"])),
        tenant_id=row["tenant_id"],
        conversation_hash=row["conversation_hash"],
        provider=row["provider"],
        model=row["model"],
        context_policy=row.get("context_policy") or "conversation",
        material_ids=[str(x) for x in material_manifest] if isinstance(material_manifest, list) else [],
        history_version=int(row.get("history_version") or 0),
        active_request_id=UUID(row["active_request_id"]) if row.get("active_request_id") else None,
        execution_pool=row.get("execution_pool") or "railway-default",
        route_revision=str(route.get("route_revision")) if route.get("route_revision") else None,
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
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    route = metadata.get("_relay_route") if isinstance(metadata.get("_relay_route"), dict) else {}
    intent = metadata.get("_relay_material_intent") if isinstance(metadata.get("_relay_material_intent"), dict) else {}
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
    public_provider = str(intent.get("provider") or (binding or {}).get("provider") or "") or None
    if binding and str(binding.get("state") or binding.get("processing_state") or "").lower() in {"active", "ready", "processed"}:
        if public_provider:
            ready_for.append(public_provider)
    elif status_value == "ready" and fallback and public_provider:
        ready_for.append(public_provider)
    object_id = fallback.get("object_id") if fallback else row.get("object_id")
    storage_id = fallback.get("storage_id") if fallback else None
    provider_binding = None
    if binding:
        provider_binding = {
            "provider": binding.get("provider"),
            "state": str(binding.get("state") or binding.get("processing_state") or "unknown"),
            "generation": int(binding.get("generation") or 1),
            "purpose": binding.get("purpose"),
            "representation": binding.get("representation"),
            "expires_at": _dt(binding.get("expires_at")),
            "route_revision": route.get("route_revision"),
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
        provider=public_provider,
        model=str(intent.get("model")) if intent.get("model") else None,
        purpose=str(intent.get("purpose") or "inference_input"),
        route_revision=str(route.get("route_revision")) if route.get("route_revision") else None,
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


async def _validate_materials_for_route(
    request: Request,
    *,
    material_ids: list[str],
    tenant_id: str,
    conversation_hash: str,
    route: RouteBinding,
) -> None:
    repo = _repo(request)
    file_adapter = _provider_files(request).maybe_get(route.connection_id)
    for material_id in material_ids:
        material = await repo.get_material(
            material_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        if not material or material.get("status") in {
            "failed", "deleted", "reupload_required", "receiving", "binding"
        }:
            log_warning(
                logger,
                "material_route_validation_failed",
                material_id=material_id,
                provider=route.provider,
                model=route.model,
                route_revision=route.route_revision,
                connection_id=route.connection_id,
                reason="material_not_usable",
                failure_class="client",
            )
            raise HTTPException(status_code=409, detail={
                "code": "MATERIAL_NOT_USABLE",
                "material_id": material_id,
            })

        desired_binding = None
        if file_adapter is not None:
            desired_binding = await repo.get_provider_binding(
                material_id=material_id,
                connection_id=route.connection_id,
                account_scope_hash=file_adapter.account_scope_hash,
            )
            if desired_binding and str(desired_binding.get("state") or desired_binding.get("processing_state") or "").lower() in {
                "active", "ready", "processed"
            }:
                continue

        fallback = await repo.get_material_fallback(material_id)
        if fallback:
            if str(material.get("target_connection_id") or "") != route.connection_id:
                log_warning(
                    logger,
                    "material_route_rebind_required",
                    material_id=material_id,
                    provider=route.provider,
                    model=route.model,
                    route_revision=route.route_revision,
                    connection_id=route.connection_id,
                    existing_connection_id=material.get("target_connection_id"),
                    fallback_available=True,
                )
            continue

        log_error(
            logger,
            "material_route_validation_failed",
            material_id=material_id,
            provider=route.provider,
            model=route.model,
            route_revision=route.route_revision,
            connection_id=route.connection_id,
            existing_connection_id=material.get("target_connection_id"),
            reason="no_binding_for_route_and_no_fallback",
            failure_class="client",
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ROUTE_BINDING_MISMATCH",
                "message": "Material is not bound to the Session route and has no fallback bytes for rebinding",
                "material_id": material_id,
                "recovery": "MATERIAL_REUPLOAD_REQUIRED",
            },
        )


@router.post("/materials", response_model=MaterialResponse, status_code=status.HTTP_201_CREATED)
async def create_material(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
    request_file_total_bytes_header: str | None = Header(
        default=None, alias="X-Relay-Request-File-Total-Bytes"
    ),
    request_file_count_header: str | None = Header(
        default=None, alias="X-Relay-Request-File-Count"
    ),
    material_batch_id_header: str | None = Header(
        default=None, alias="X-Relay-Material-Batch-Id"
    ),
) -> MaterialResponse:
    ingress_id = f"ing_{uuid4().hex}"
    api_started_ms = now_ms()
    content_type_header = str(request.headers.get("content-type") or "").lower()
    caller_version = request.headers.get("x-relay-client-version")
    log_info(
        logger,
        "material_upload_received",
        ingress_id=ingress_id,
        method=request.method,
        media_type=content_type_header.split(";", 1)[0] if content_type_header else None,
        caller_version=caller_version,
    )

    # Direct unit calls invoke the route function without FastAPI dependency
    # injection, in which case Header() defaults are FieldInfo objects rather
    # than actual header values. Normalize those to None before parsing.
    if not isinstance(request_file_total_bytes_header, (str, int)):
        request_file_total_bytes_header = None
    if not isinstance(request_file_count_header, (str, int)):
        request_file_count_header = None
    if not isinstance(material_batch_id_header, str):
        material_batch_id_header = None

    data: bytes | None = None
    tenant_id = ""
    conversation_hash = ""
    filename: str | None = None
    content_type: str | None = None
    source_url: str | None = None
    source_ref: str | None = None
    parent_material_id: str | None = None
    ordinal: int | None = None
    metadata: dict[str, Any] = {}
    legacy_target_connection_id: str | None = None
    provider: str | None = None
    model: str | None = None
    purpose = "inference_input"
    durability_policy = _settings(request).material_default_durability_policy
    fallback_policy = _settings(request).material_default_fallback_policy
    declared_size: int | None = None
    request_file_total_bytes: int | None = None
    request_file_count: int | None = None
    material_batch_id: str | None = material_batch_id_header

    try:
        if request_file_total_bytes_header not in (None, ""):
            request_file_total_bytes = int(request_file_total_bytes_header)
            if request_file_total_bytes < 0:
                raise ValueError("X-Relay-Request-File-Total-Bytes must be >= 0")
        if request_file_count_header not in (None, ""):
            request_file_count = int(request_file_count_header)
            if request_file_count < 1:
                raise ValueError("X-Relay-Request-File-Count must be >= 1")
        if material_batch_id is not None and len(material_batch_id) > 220:
            raise ValueError("X-Relay-Material-Batch-Id exceeds 220 characters")
        if content_type_header.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise ValueError("multipart material requires file field")
            chunks: list[bytes] = []
            total = 0
            max_bytes = _settings(request).material_ingress_max_bytes
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("material exceeds configured ingress size limit")
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
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be a JSON object")
            provider = str(form.get("provider") or "") or None
            model = str(form.get("model") or "") or None
            purpose = str(form.get("purpose") or "inference_input")
            legacy_target_connection_id = str(form.get("target_connection_id") or "") or None
            durability_policy = str(form.get("durability_policy") or _settings(request).material_default_durability_policy)
            fallback_policy = str(form.get("fallback_policy") or _settings(request).material_default_fallback_policy)
            declared_size = int(form["declared_size"]) if form.get("declared_size") not in (None, "") else None
            if form.get("request_file_total_bytes") not in (None, ""):
                request_file_total_bytes = int(form["request_file_total_bytes"])
            if form.get("request_file_count") not in (None, ""):
                request_file_count = int(form["request_file_count"])
            material_batch_id = str(form.get("material_batch_id") or material_batch_id or "") or None
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
            provider = payload.provider
            model = payload.model
            purpose = payload.purpose
            legacy_target_connection_id = payload.target_connection_id
            durability_policy = payload.durability_policy
            fallback_policy = payload.fallback_policy
            declared_size = payload.declared_size
            request_file_total_bytes = (
                payload.request_file_total_bytes
                if payload.request_file_total_bytes is not None
                else request_file_total_bytes
            )
            request_file_count = (
                payload.request_file_count
                if payload.request_file_count is not None
                else request_file_count
            )
            material_batch_id = payload.material_batch_id or material_batch_id
            if payload.content_base64:
                data = base64.b64decode(payload.content_base64, validate=True)
    except ValidationError as exc:
        log_warning(
            logger,
            "material_request_parse_failed",
            exc_info=True,
            ingress_id=ingress_id,
            phase="schema_validation",
            http_status=422,
            failure_class="client",
            exception_type=type(exc).__name__,
            validation_errors=_validation_error_summary(exc),
            duration_ms=elapsed_ms(api_started_ms),
        )
        log_warning(
            logger,
            "material_upload_failed",
            exc_info=True,
            ingress_id=ingress_id,
            phase="schema_validation",
            http_status=422,
            failure_class="client",
            exception_type=type(exc).__name__,
            duration_ms=elapsed_ms(api_started_ms),
        )
        raise HTTPException(status_code=422, detail=_validation_error_summary(exc)) from exc
    except Exception as exc:
        log_warning(
            logger,
            "material_request_parse_failed",
            exc_info=True,
            ingress_id=ingress_id,
            phase="multipart_or_json_parse",
            http_status=400,
            failure_class="client",
            exception_type=type(exc).__name__,
            message=str(exc),
            duration_ms=elapsed_ms(api_started_ms),
        )
        log_warning(
            logger,
            "material_upload_failed",
            exc_info=True,
            ingress_id=ingress_id,
            phase="multipart_or_json_parse",
            http_status=400,
            failure_class="client",
            exception_type=type(exc).__name__,
            duration_ms=elapsed_ms(api_started_ms),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not tenant_id or not conversation_hash:
        log_warning(
            logger,
            "material_request_validation_failed",
            ingress_id=ingress_id,
            phase="owner_validation",
            http_status=400,
            failure_class="client",
            reason="tenant_id and conversation_hash are required",
            duration_ms=elapsed_ms(api_started_ms),
        )
        log_warning(
            logger,
            "material_upload_failed",
            ingress_id=ingress_id,
            phase="owner_validation",
            http_status=400,
            failure_class="client",
            duration_ms=elapsed_ms(api_started_ms),
        )
        raise HTTPException(status_code=400, detail="tenant_id and conversation_hash are required")
    if purpose not in {"inference_input", "archive"}:
        log_warning(
            logger,
            "material_request_validation_failed",
            ingress_id=ingress_id,
            phase="purpose_validation",
            http_status=400,
            failure_class="client",
            reason="purpose must be inference_input or archive",
            purpose=purpose,
            duration_ms=elapsed_ms(api_started_ms),
        )
        log_warning(
            logger,
            "material_upload_failed",
            ingress_id=ingress_id,
            phase="purpose_validation",
            http_status=400,
            failure_class="client",
            purpose=purpose,
            duration_ms=elapsed_ms(api_started_ms),
        )
        raise HTTPException(status_code=400, detail="purpose must be inference_input or archive")

    route: RouteBinding | None = None
    if purpose == "inference_input":
        if not provider or not model:
            log_warning(
                logger,
                "material_request_validation_failed",
                ingress_id=ingress_id,
                phase="route_intent_validation",
                http_status=400,
                failure_class="client",
                provider=provider,
                model=model,
                reason="provider and model are required for inference_input material",
                duration_ms=elapsed_ms(api_started_ms),
            )
            log_warning(
                logger,
                "material_upload_failed",
                ingress_id=ingress_id,
                phase="route_intent_validation",
                http_status=400,
                failure_class="client",
                provider=provider,
                model=model,
                duration_ms=elapsed_ms(api_started_ms),
            )
            raise HTTPException(status_code=400, detail="provider and model are required for inference_input material")
        try:
            route = _route_resolver(request).resolve(
                provider=provider, model=model, purpose="material_ingress"
            )
            _route_resolver(request).handle_legacy_hint(
                client_hint=legacy_target_connection_id,
                resolved=route,
                caller_version=caller_version,
                scope="material",
            )
        except RouteResolutionError as exc:
            log_error(
                logger,
                "material_upload_failed",
                ingress_id=ingress_id,
                phase="route_resolution",
                provider=provider,
                model=model,
                http_status=409,
                failure_class="relay_configuration" if exc.code != "LEGACY_CONNECTION_HINT_MISMATCH" else "client",
                error_code=exc.code,
                exception_type=type(exc).__name__,
                duration_ms=elapsed_ms(api_started_ms),
            )
            raise HTTPException(status_code=409, detail=exc.public_detail()) from exc
    elif legacy_target_connection_id:
        log_warning(
            logger,
            "legacy_connection_hint_ignored",
            ingress_id=ingress_id,
            scope="material_archive",
            client_hint=legacy_target_connection_id,
            purpose=purpose,
        )

    try:
        row = await _material_ingress(request).create(
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            idempotency_key=idempotency_key,
            filename=str(filename or "material.bin"),
            content_type=content_type,
            data=data,
            source_url=source_url,
            source_ref=source_ref,
            parent_material_id=parent_material_id,
            ordinal=ordinal,
            metadata=metadata,
            route_binding=route,
            durability_policy=durability_policy,
            fallback_policy=fallback_policy,
            declared_size=declared_size,
            request_file_total_bytes=request_file_total_bytes,
            request_file_count=request_file_count,
            material_batch_id=material_batch_id,
            ingress_id=ingress_id,
            purpose=purpose,
        )
    except MaterialIngressError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc)}
        source = str(detail.get("source") or "relay")
        failure_class = "relay"
        if source == "provider_file_api":
            upstream_status = detail.get("upstream_http_status")
            failure_class = "upstream_rejected" if isinstance(upstream_status, int) and 400 <= upstream_status < 500 else "upstream"
        elif source == "material_source":
            failure_class = "source_rejected" if isinstance(detail.get("upstream_http_status"), int) and 400 <= int(detail["upstream_http_status"]) < 500 else "source"
        elif exc.status_code < 500:
            failure_class = "client"
        log_error(
            logger,
            "material_upload_failed",
            exc_info=True,
            ingress_id=exc.ingress_id or ingress_id,
            material_id=exc.material_id,
            provider=provider,
            model=model,
            purpose=purpose,
            route_revision=route.route_revision if route else None,
            connection_id=route.connection_id if route else None,
            phase=detail.get("phase") or "material_ingress",
            http_status=exc.status_code,
            upstream_http_status=detail.get("upstream_http_status"),
            failure_class=failure_class,
            error_code=detail.get("code"),
            exception_type=type(exc).__name__,
            duration_ms=elapsed_ms(api_started_ms),
        )
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    except Exception as exc:
        log_error(
            logger,
            "material_upload_failed",
            exc_info=True,
            ingress_id=ingress_id,
            provider=provider,
            model=model,
            purpose=purpose,
            route_revision=route.route_revision if route else None,
            connection_id=route.connection_id if route else None,
            phase="material_ingress",
            http_status=502,
            failure_class="relay",
            exception_type=type(exc).__name__,
            duration_ms=elapsed_ms(api_started_ms),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    log_info(
        logger,
        "material_upload_completed",
        ingress_id=ingress_id,
        material_id=row.get("id"),
        provider=provider,
        model=model,
        purpose=purpose,
        route_revision=route.route_revision if route else None,
        connection_id=row.get("target_connection_id"),
        status=row.get("status"),
        durability=row.get("durability"),
        size_bytes=row.get("actual_size") or row.get("size_bytes"),
        content_type=row.get("content_type"),
        request_file_total_bytes=request_file_total_bytes,
        request_file_count=request_file_count,
        material_batch_id=material_batch_id,
        duration_ms=elapsed_ms(api_started_ms),
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
    settings = _settings(request)
    caller_version = request.headers.get("x-relay-client-version")
    session_intent = {
        "tenant_id": body.tenant_id,
        "conversation_hash": body.conversation_hash,
        "provider": body.provider,
        "model": body.model,
        "context_policy": body.context_policy,
        "material_ids": body.material_ids,
        "execution_pool": body.execution_pool or settings.execution_pool,
        "metadata": body.metadata,
    }
    session_hash = stable_hash(session_intent)
    legacy_session_hash = stable_hash(body.model_dump(mode="json"))
    existing = await repo.find_session_by_idempotency(body.tenant_id, idempotency_key)
    if existing:
        if existing.get("session_hash") not in {session_hash, legacy_session_hash}:
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

    try:
        route = _route_resolver(request).resolve(
            provider=body.provider, model=body.model, purpose="session"
        )
        _route_resolver(request).handle_legacy_hint(
            client_hint=body.connection_id,
            resolved=route,
            caller_version=caller_version,
            scope="session",
        )
    except RouteResolutionError as exc:
        raise HTTPException(status_code=409, detail=exc.public_detail()) from exc

    if body.execution_pool and body.execution_pool != route.execution_pool:
        log_warning(
            logger,
            "session_execution_pool_mismatch",
            provider=route.provider,
            model=route.model,
            route_revision=route.route_revision,
            requested_pool=body.execution_pool,
            resolved_pool=route.execution_pool,
            deployment_id=settings.deployment_id,
            failure_class="client",
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXECUTION_POOL_MISMATCH",
                "requested_pool": body.execution_pool,
                "local_pool": route.execution_pool,
                "deployment_id": settings.deployment_id,
            },
        )

    await _validate_materials_for_route(
        request,
        material_ids=body.material_ids,
        tenant_id=body.tenant_id,
        conversation_hash=body.conversation_hash,
        route=route,
    )

    session_id = uuid4()
    session_metadata = dict(body.metadata)
    session_metadata["_relay_route"] = route.internal_metadata()
    row = await repo.create_session(
        {
            "id": str(session_id),
            "tenant_id": body.tenant_id,
            "conversation_hash": body.conversation_hash,
            "provider": route.provider,
            "connection_id": route.connection_id,
            "model": route.model,
            "prompt_cache_key": stable_prompt_cache_key(body.conversation_hash),
            "material_prefix_object_path": None,
            "history_object_path": None,
            "history_object_id": None,
            "signed_url_expires_at": None,
            "history_version": 0,
            "context_policy": body.context_policy,
            "material_manifest": body.material_ids,
            "active_request_id": None,
            "execution_pool": route.execution_pool,
            "protocol_version": "v2.1",
            "idempotency_key": idempotency_key,
            "session_hash": session_hash,
            "metadata": session_metadata,
            "expires_at": repo.default_session_expiry().isoformat(),
        }
    )
    log_info(
        logger,
        "session_route_frozen",
        session_id=row.get("id"),
        provider=route.provider,
        model=route.model,
        route_revision=route.route_revision,
        route_binding_hash=route.route_binding_hash,
        connection_id=route.connection_id,
        adapter_version=route.inference_adapter_version,
        file_adapter_version=route.file_adapter_version,
        execution_pool=route.execution_pool,
    )
    log_info(
        logger,
        "session_created",
        session_id=row.get("id"),
        provider=row.get("provider"),
        connection_id=row.get("connection_id"),
        model=row.get("model"),
        route_revision=route.route_revision,
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

    provider = str(session["provider"])
    connection_id = str(session.get("connection_id") or "")
    model = str(session["model"])
    caller_version = request.headers.get("x-relay-client-version")
    legacy_mismatches: dict[str, Any] = {}
    if body.provider is not None and str(body.provider) != provider:
        legacy_mismatches["provider"] = {"client": body.provider, "session": provider}
    if body.model is not None and str(body.model) != model:
        legacy_mismatches["model"] = {"client": body.model, "session": model}
    if body.connection_id is not None and str(body.connection_id) != connection_id:
        legacy_mismatches["connection_id"] = {"client": body.connection_id, "session": connection_id}
    if legacy_mismatches:
        mode = str(settings.route_legacy_hint_mode or "warn").lower()
        log_warning(
            logger,
            "legacy_request_route_hint_mismatch",
            session_id=str(session_id),
            provider=provider,
            model=model,
            connection_id=connection_id,
            caller_version=caller_version,
            mismatches=legacy_mismatches,
            enforcement="reject" if mode == "strict" else "ignored",
        )
        if mode == "strict":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "LEGACY_CONNECTION_HINT_MISMATCH",
                    "message": "Request route hints do not match the frozen Session route",
                },
            )
    effective_material_ids = list(body.material_ids)
    if session.get("context_policy") == "conversation" and isinstance(session.get("material_manifest"), list):
        effective_material_ids = [str(x) for x in session.get("material_manifest") or []] + effective_material_ids
    effective_material_ids = list(dict.fromkeys(effective_material_ids))
    session_metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    frozen_route = session_metadata.get("_relay_route") if isinstance(session_metadata.get("_relay_route"), dict) else {}
    if frozen_route:
        session_route = RouteBinding(
            provider=provider,
            model=model,
            connection_id=connection_id,
            route_revision=str(frozen_route.get("route_revision") or "legacy"),
            route_binding_hash=str(frozen_route.get("route_binding_hash") or ""),
            account_scope_hash=str(frozen_route.get("account_scope_hash") or ""),
            inference_adapter_version=str(frozen_route.get("inference_adapter_version") or "unknown"),
            file_adapter_version=(str(frozen_route.get("file_adapter_version")) if frozen_route.get("file_adapter_version") else None),
            execution_pool=str(session.get("execution_pool") or settings.execution_pool),
            purpose="request",
        )
        await _validate_materials_for_route(
            request,
            material_ids=effective_material_ids,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            route=session_route,
        )
    material_hashes: dict[str, str] = {}
    for material_id in effective_material_ids:
        material = await repo.get_material(
            material_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        if not material or material.get("status") in {"failed", "deleted", "reupload_required", "receiving", "binding"}:
            raise HTTPException(status_code=409, detail=f"Material is not usable: {material_id}")
        material_hashes[material_id] = str(material.get("sha256") or "")

    snapshot = {
        "schema_version": "relay-request/2.1",
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
        "route_revision": frozen_route.get("route_revision") if frozen_route else None,
        "route_binding_hash": frozen_route.get("route_binding_hash") if frozen_route else None,
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
            route_revision=frozen_route.get("route_revision") if frozen_route else None,
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
        route_revision=frozen_route.get("route_revision") if frozen_route else None,
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
