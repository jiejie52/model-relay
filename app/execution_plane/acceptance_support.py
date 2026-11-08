from __future__ import annotations

import hashlib
import logging
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.materials.document_projection import DOCX, projection_authorized
from app.observability import error as log_error, warning as log_warning
from app.routing import RouteBinding
from app.storage_paths import request_object_path_v2
from app.utils import json_bytes, utcnow


from app.execution_plane.services import ExecutionRequestError

logger = logging.getLogger("model-relay-execution-plane")

async def _store_request_json_artifact(
    services,
    *,
    tenant_id: str,
    conversation_hash: str,
    session_id: str,
    request_id: str,
    filename: str,
    value: Any,
) -> str:
    """Persist an immutable Request-side JSON artifact and return relay_objects.id."""
    object_id = f"obj_{uuid4().hex}"
    raw = json_bytes(value)
    digest = hashlib.sha256(raw).hexdigest()
    path = request_object_path_v2(
        services.settings, tenant_id, conversation_hash, session_id, request_id, filename
    )
    backend = services.storage.get(services.settings.default_storage_id)
    location = await backend.put_bytes(path, raw, content_type="application/json")
    await services.repo.create_object(
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
    return object_id


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


def _material_modality(content_type: Any) -> str:
    mime = str(content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if mime.startswith("text/") or mime in {"application/json", "application/xml", "application/yaml", "application/x-yaml"}:
        return "text"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf" or mime.startswith("application/"):
        return "document"
    return "binary"


async def _validate_materials_for_route(
    services,
    *,
    material_ids: list[str],
    tenant_id: str,
    conversation_hash: str,
    route: RouteBinding,
) -> None:
    repo = services.repo
    file_adapter = services.provider_files.maybe_get(route.connection_id)
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
            raise ExecutionRequestError(status_code=409, detail={
                "code": "MATERIAL_NOT_USABLE",
                "material_id": material_id,
            })

        contract = route.capability_contract if isinstance(route.capability_contract, dict) else {}
        allowed_modalities = {
            str(x).strip().lower()
            for x in (contract.get("input_modalities") or [])
            if str(x).strip()
        }
        modality = _material_modality(material.get("content_type"))
        if (str(material.get("content_type") or "").split(";", 1)[0].lower() == DOCX
                and projection_authorized(contract)):
            modality = "text"
        if allowed_modalities and modality not in allowed_modalities:
            log_warning(
                logger,
                "material_capability_validation_failed",
                material_id=material_id,
                provider=route.provider,
                model=route.model,
                offering_id=route.offering_id,
                channel_id=route.channel_id,
                content_type=material.get("content_type"),
                modality=modality,
                allowed_modalities=sorted(allowed_modalities),
                failure_class="client",
            )
            raise ExecutionRequestError(
                status_code=409,
                detail={
                    "code": "MATERIAL_MODALITY_UNSUPPORTED",
                    "material_id": material_id,
                    "modality": modality,
                },
            )

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
        raise ExecutionRequestError(
            status_code=409,
            detail={
                "code": "ROUTE_BINDING_MISMATCH",
                "message": "Material is not bound to the Session route and has no fallback bytes for rebinding",
                "material_id": material_id,
                "recovery": "MATERIAL_REUPLOAD_REQUIRED",
            },
        )

