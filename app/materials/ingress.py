from __future__ import annotations

import hashlib
import logging
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from ..config import Settings
from ..core.raw_error import raw_body_inline_fields
from ..providers.base import ProviderHTTPError, ProviderRequestError
from ..storage_paths import relay_object_path
from ..utils import utcnow
from ..observability import (
    elapsed_ms,
    error as log_error,
    info as log_info,
    warning as log_warning,
    now_ms,
    exception_failure_class,
)
from ..v2_repository import RelayV2Repository
from .fallback_storage import FallbackObjectStorage
from .provider_files.base import MaterialFile
from .provider_files.registry import ProviderFileRegistry
from .safe_fetch import MaterialFetchError, fetch_bytes

if TYPE_CHECKING:
    from ..routing.route_resolver import RouteBinding


logger = logging.getLogger("model-relay-materials")


class MaterialIngressError(RuntimeError):
    def __init__(
        self,
        detail: dict[str, Any],
        *,
        status_code: int = 502,
        ingress_id: str | None = None,
        material_id: str | None = None,
    ) -> None:
        super().__init__(str(detail.get("message") or detail.get("code") or "material ingress failed"))
        self.detail = detail
        self.status_code = status_code
        self.ingress_id = ingress_id
        self.material_id = material_id


class MaterialIngress:
    """Provider-native-first material ingress with full lifecycle observability.

    Public callers express provider/model intent. ``route_binding`` is resolved
    by Relay before this class is called. The legacy ``target_connection_id``
    argument remains only for internal/unit compatibility and is never sourced
    from the public API route decision in contract 2.1.
    """

    def __init__(
        self,
        repo: RelayV2Repository,
        fallback: FallbackObjectStorage,
        file_adapters: ProviderFileRegistry,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.fallback = fallback
        self.file_adapters = file_adapters
        self.settings = settings

    async def create(
        self,
        *,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
        filename: str,
        content_type: str | None,
        data: bytes | None = None,
        source_url: str | None = None,
        source_ref: str | None = None,
        parent_material_id: str | None = None,
        ordinal: int | None = None,
        metadata: dict[str, Any] | None = None,
        route_binding: "RouteBinding | None" = None,
        target_connection_id: str | None = None,
        durability_policy: str | None = None,
        fallback_policy: str | None = None,
        declared_size: int | None = None,
        ingress_id: str | None = None,
        purpose: str = "inference_input",
    ) -> dict[str, Any]:
        ingress_id = ingress_id or f"ing_{uuid4().hex}"
        ingress_started_ms = now_ms()
        resolved_connection_id = route_binding.connection_id if route_binding is not None else target_connection_id
        provider = route_binding.provider if route_binding is not None else None
        model = route_binding.model if route_binding is not None else None
        route_revision = route_binding.route_revision if route_binding is not None else None
        source_kind = "readable_url" if source_url is not None else "inline"

        log_info(
            logger,
            "material_ingress_started",
            ingress_id=ingress_id,
            filename=filename,
            declared_content_type=content_type,
            declared_size=declared_size,
            provider=provider,
            model=model,
            purpose=purpose,
            connection_id=resolved_connection_id,
            route_revision=route_revision,
            source_kind=source_kind,
        )

        existing = await self.repo.find_material_by_idempotency(tenant_id, idempotency_key)
        if existing:
            existing_connection = existing.get("target_connection_id")
            if resolved_connection_id and existing_connection and str(existing_connection) != str(resolved_connection_id):
                detail = {
                    "source": "relay",
                    "code": "IDEMPOTENCY_ROUTE_CONFLICT",
                    "message": "Idempotency-Key already identifies a Material bound to a different Relay route",
                }
                log_error(
                    logger,
                    "material_ingress_idempotency_route_conflict",
                    ingress_id=ingress_id,
                    material_id=existing.get("id"),
                    provider=provider,
                    model=model,
                    route_revision=route_revision,
                    connection_id=resolved_connection_id,
                    existing_connection_id=existing_connection,
                    failure_class="client",
                )
                raise MaterialIngressError(
                    detail,
                    status_code=409,
                    ingress_id=ingress_id,
                    material_id=str(existing.get("id") or "") or None,
                )
            log_info(
                logger,
                "material_ingress_idempotent_reuse",
                ingress_id=ingress_id,
                material_id=existing.get("id"),
                provider=provider,
                model=model,
                route_revision=route_revision,
                connection_id=existing.get("target_connection_id"),
                status=existing.get("status"),
                duration_ms=elapsed_ms(ingress_started_ms),
            )
            return existing

        durability_policy = (durability_policy or self.settings.material_default_durability_policy).lower()
        fallback_policy = (fallback_policy or self.settings.material_default_fallback_policy).lower()
        try:
            if durability_policy not in {"native_first", "relay_backed"}:
                raise ValueError("durability_policy must be native_first or relay_backed")
            if fallback_policy not in {"never", "on_provider_unavailable", "always"}:
                raise ValueError("fallback_policy must be never, on_provider_unavailable or always")
        except ValueError as exc:
            log_warning(
                logger,
                "material_policy_validation_failed",
                exc_info=True,
                ingress_id=ingress_id,
                provider=provider,
                model=model,
                purpose=purpose,
                route_revision=route_revision,
                connection_id=resolved_connection_id,
                durability_policy=durability_policy,
                fallback_policy=fallback_policy,
                failure_class="client",
                exception_type=type(exc).__name__,
                message=str(exc),
            )
            raise MaterialIngressError(
                {"source": "relay", "code": "MATERIAL_POLICY_INVALID", "message": str(exc)},
                status_code=400,
                ingress_id=ingress_id,
            ) from exc

        detected_content_type = content_type
        if source_url is not None:
            fetch_started_ms = now_ms()
            safe_source = self._sanitized_source_url(source_url)
            log_info(
                logger,
                "material_source_fetch_started",
                ingress_id=ingress_id,
                provider=provider,
                model=model,
                route_revision=route_revision,
                source_url=safe_source,
                max_bytes=self.settings.material_ingress_max_bytes,
            )
            try:
                data, fetched_content_type = await fetch_bytes(
                    source_url,
                    max_bytes=self.settings.material_ingress_max_bytes,
                    timeout_seconds=self.settings.material_ingress_timeout_seconds,
                    allow_http=self.settings.material_allow_http,
                )
                detected_content_type = detected_content_type or fetched_content_type
                log_info(
                    logger,
                    "material_source_fetch_completed",
                    ingress_id=ingress_id,
                    provider=provider,
                    model=model,
                    source_url=safe_source,
                    duration_ms=elapsed_ms(fetch_started_ms),
                    size_bytes=len(data),
                    content_type=fetched_content_type,
                )
            except Exception as exc:
                detail = self._source_error_detail(exc)
                log_error(
                    logger,
                    "material_source_fetch_failed",
                    exc_info=True,
                    ingress_id=ingress_id,
                    provider=provider,
                    model=model,
                    route_revision=route_revision,
                    connection_id=resolved_connection_id,
                    source_url=safe_source,
                    phase=getattr(exc, "phase", "source_fetch"),
                    duration_ms=elapsed_ms(fetch_started_ms),
                    failure_class=self._source_failure_class(exc),
                    upstream_http_status=getattr(exc, "status_code", None),
                    bytes_received=getattr(exc, "bytes_received", None),
                    exception_type=type(exc).__name__,
                    error_code=getattr(exc, "code", None),
                )
                status_code = 400 if getattr(exc, "code", "") in {
                    "MATERIAL_SOURCE_POLICY_REJECTED",
                    "MATERIAL_SOURCE_TOO_LARGE",
                } else 502
                raise MaterialIngressError(
                    detail,
                    status_code=status_code,
                    ingress_id=ingress_id,
                ) from exc

        try:
            if data is None:
                raise ValueError("material data is required")
            if len(data) > self.settings.material_ingress_max_bytes:
                raise ValueError("material exceeds configured ingress size limit")
            if declared_size is not None and int(declared_size) != len(data):
                raise ValueError(
                    f"declared_size does not match received bytes: declared={declared_size} actual={len(data)}"
                )
        except ValueError as exc:
            log_warning(
                logger,
                "material_payload_validation_failed",
                exc_info=True,
                ingress_id=ingress_id,
                provider=provider,
                model=model,
                purpose=purpose,
                route_revision=route_revision,
                connection_id=resolved_connection_id,
                actual_size=len(data) if data is not None else None,
                declared_size=declared_size,
                failure_class="client",
                exception_type=type(exc).__name__,
                message=str(exc),
            )
            raise MaterialIngressError(
                {"source": "relay", "code": "MATERIAL_PAYLOAD_INVALID", "message": str(exc)},
                status_code=400,
                ingress_id=ingress_id,
            ) from exc

        detected_content_type = (detected_content_type or "application/octet-stream").split(";", 1)[0].strip()
        digest = hashlib.sha256(data).hexdigest()
        material_id = f"mat_{uuid4().hex}"
        route_meta = route_binding.internal_metadata() if route_binding is not None else None
        material_metadata = dict(metadata or {})
        if route_meta is not None:
            material_metadata["_relay_route"] = route_meta
        material_metadata["_relay_material_intent"] = {
            "provider": provider,
            "model": model,
            "purpose": purpose,
        }
        try:
            row = await self.repo.create_material(
                {
                    "id": material_id,
                    "tenant_id": tenant_id,
                    "conversation_hash": conversation_hash,
                    "status": "binding" if resolved_connection_id else "receiving",
                    "idempotency_key": idempotency_key,
                    "filename": filename,
                    "content_type": detected_content_type,
                    "size_bytes": len(data),
                    "declared_size": declared_size,
                    "actual_size": len(data),
                    "sha256": digest,
                    "object_id": None,
                    "source_kind": source_kind,
                    "source_ref": source_ref,
                    "source_url": self._sanitized_source_url(source_url),
                    "source_recoverable": False,
                    "target_connection_id": resolved_connection_id,
                    "durability_policy": durability_policy,
                    "fallback_policy": fallback_policy,
                    "durability": "provider_bound" if resolved_connection_id else "relay_backed",
                    "parent_material_id": parent_material_id,
                    "ordinal": ordinal,
                    "metadata": material_metadata,
                    "created_at": utcnow().isoformat(),
                }
            )
        except Exception as exc:
            log_error(
                logger,
                "material_registry_create_failed",
                exc_info=True,
                ingress_id=ingress_id,
                material_id=material_id,
                provider=provider,
                model=model,
                purpose=purpose,
                route_revision=route_revision,
                connection_id=resolved_connection_id,
                failure_class="dependency" if "supabase" in type(exc).__name__.lower() else "relay",
                exception_type=type(exc).__name__,
                duration_ms=elapsed_ms(ingress_started_ms),
            )
            raise

        if route_binding is not None:
            log_info(
                logger,
                "material_route_bound",
                ingress_id=ingress_id,
                material_id=material_id,
                provider=route_binding.provider,
                model=route_binding.model,
                purpose=purpose,
                route_revision=route_binding.route_revision,
                connection_id=route_binding.connection_id,
                file_adapter_version=route_binding.file_adapter_version,
                account_scope_hash=route_binding.account_scope_hash,
            )

        adapter = self.file_adapters.maybe_get(resolved_connection_id)
        fallback_row: dict[str, Any] | None = None

        if adapter is None:
            fallback_row = await self._store_fallback_logged(
                ingress_id=ingress_id,
                row=row,
                data=data,
                retention_policy="bridge" if resolved_connection_id else "archive",
                provider=provider,
                connection_id=resolved_connection_id,
            )
            updated = await self.repo.update_material(
                material_id,
                {
                    "status": "ready",
                    "object_id": fallback_row["object_id"],
                    "durability": "relay_backed",
                },
            ) or row
            log_info(
                logger,
                "material_ingress_completed",
                ingress_id=ingress_id,
                material_id=material_id,
                provider=provider,
                model=model,
                connection_id=resolved_connection_id,
                route_revision=route_revision,
                status="ready",
                durability="relay_backed",
                fallback_stored=True,
                duration_ms=elapsed_ms(ingress_started_ms),
            )
            return updated

        generation = 1
        attempt_id = f"mba_{uuid4().hex}"
        await self._safe_create_attempt(
            {
                "attempt_id": attempt_id,
                "material_id": material_id,
                "connection_id": resolved_connection_id,
                "account_scope_hash": adapter.account_scope_hash,
                "generation": generation,
                "phase": "started",
                "uncertain": False,
                "detail": {"provider": adapter.provider, "adapter_version": adapter.adapter_version},
                "created_at": utcnow().isoformat(),
            },
            ingress_id=ingress_id,
        )

        material_file = MaterialFile(
            material_id=material_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            filename=filename,
            content_type=detected_content_type,
            size_bytes=len(data),
            sha256=digest,
            data=data,
        )
        binding_started_ms = now_ms()
        log_info(
            logger,
            "provider_file_binding_started",
            ingress_id=ingress_id,
            material_id=material_id,
            provider=adapter.provider,
            connection_id=resolved_connection_id,
            adapter_version=adapter.adapter_version,
            route_revision=route_revision,
            generation=generation,
            size_bytes=len(data),
            content_type=detected_content_type,
        )
        try:
            result = await adapter.prepare(material_file, generation=generation)
            binding = dict(result.binding)
            binding["material_id"] = material_id
            binding.setdefault("created_at", utcnow().isoformat())
            binding["updated_at"] = utcnow().isoformat()
            await self.repo.upsert_provider_binding(binding)
            response_ref = await self._archive_attempt_payload(
                row,
                attempt_id,
                result.raw_response,
                result.raw_response_content_type or "application/octet-stream",
                "provider-response.bin",
            )
            await self._safe_update_attempt(
                attempt_id,
                {
                    "phase": result.phase,
                    "provider_request_id": result.request_id,
                    "raw_response_ref": response_ref,
                    "uncertain": False,
                    "completed_at": utcnow().isoformat(),
                },
                ingress_id=ingress_id,
                material_id=material_id,
            )
            log_info(
                logger,
                "provider_file_binding_completed",
                ingress_id=ingress_id,
                material_id=material_id,
                provider=adapter.provider,
                connection_id=resolved_connection_id,
                adapter_version=adapter.adapter_version,
                route_revision=route_revision,
                generation=generation,
                phase=result.phase,
                http_status=result.http_status,
                upstream_request_id=result.request_id,
                duration_ms=elapsed_ms(binding_started_ms),
            )
        except Exception as exc:
            log_error(
                logger,
                "provider_file_binding_failed",
                exc_info=True,
                ingress_id=ingress_id,
                material_id=material_id,
                provider=adapter.provider,
                connection_id=resolved_connection_id,
                adapter_version=adapter.adapter_version,
                route_revision=route_revision,
                generation=generation,
                phase=getattr(exc, "phase", None),
                duration_ms=elapsed_ms(binding_started_ms),
                failure_class=exception_failure_class(exc),
                upstream_http_status=getattr(exc, "status_code", None),
                upstream_request_id=getattr(exc, "request_id", None),
                stream_interrupted=getattr(exc, "stream_interrupted", False),
                bytes_received=getattr(exc, "bytes_received", None),
                exception_type=type(exc).__name__,
            )
            error_ref = None
            raw_error = self._error_detail(exc)
            raw_error["ingress_id"] = ingress_id
            raw_error["material_id"] = material_id
            if isinstance(exc, ProviderHTTPError):
                error_ref = await self._archive_attempt_payload(
                    row,
                    attempt_id,
                    exc.body,
                    exc.content_type or "application/octet-stream",
                    "provider-error.bin",
                )
            await self._safe_update_attempt(
                attempt_id,
                {
                    "phase": getattr(exc, "phase", None) or "failed",
                    "provider_request_id": getattr(exc, "request_id", None),
                    "raw_error_ref": error_ref,
                    "uncertain": self._is_uncertain(exc),
                    "detail": raw_error,
                    "completed_at": utcnow().isoformat(),
                },
                ingress_id=ingress_id,
                material_id=material_id,
            )
            if fallback_policy in {"on_provider_unavailable", "always"} and self._fallback_eligible(exc):
                fallback_row = await self._store_fallback_logged(
                    ingress_id=ingress_id,
                    row=row,
                    data=data,
                    retention_policy="provider-unavailable",
                    provider=adapter.provider,
                    connection_id=resolved_connection_id,
                )
                log_warning(
                    logger,
                    "material_provider_fallback_activated",
                    ingress_id=ingress_id,
                    material_id=material_id,
                    provider=adapter.provider,
                    connection_id=resolved_connection_id,
                    route_revision=route_revision,
                    failure_class=exception_failure_class(exc),
                    upstream_http_status=getattr(exc, "status_code", None),
                    retention_policy="provider-unavailable",
                    duration_ms=elapsed_ms(ingress_started_ms),
                )
                updated = await self.repo.update_material(
                    material_id,
                    {
                        "status": "fallback_stored",
                        "object_id": fallback_row["object_id"],
                        "durability": "relay_backed",
                        "metadata": {**material_metadata, "last_binding_error": raw_error},
                    },
                ) or row
                log_info(
                    logger,
                    "material_ingress_completed",
                    ingress_id=ingress_id,
                    material_id=material_id,
                    provider=adapter.provider,
                    model=model,
                    connection_id=resolved_connection_id,
                    route_revision=route_revision,
                    status="fallback_stored",
                    durability="relay_backed",
                    fallback_stored=True,
                    duration_ms=elapsed_ms(ingress_started_ms),
                )
                return updated
            await self.repo.update_material(
                material_id,
                {"status": "failed", "metadata": {**material_metadata, "last_binding_error": raw_error}},
            )
            raise MaterialIngressError(
                raw_error,
                status_code=502,
                ingress_id=ingress_id,
                material_id=material_id,
            ) from exc

        if durability_policy == "relay_backed" or fallback_policy == "always":
            fallback_row = await self._store_fallback_logged(
                ingress_id=ingress_id,
                row=row,
                data=data,
                retention_policy="durability" if durability_policy == "relay_backed" else "policy-always",
                provider=adapter.provider,
                connection_id=resolved_connection_id,
            )

        log_info(
            logger,
            "material_ingress_completed",
            ingress_id=ingress_id,
            material_id=material_id,
            provider=adapter.provider,
            model=model,
            connection_id=resolved_connection_id,
            route_revision=route_revision,
            status="ready_provider",
            durability="relay_backed" if fallback_row else "provider_bound",
            fallback_stored=bool(fallback_row),
            duration_ms=elapsed_ms(ingress_started_ms),
        )
        return await self.repo.update_material(
            material_id,
            {
                "status": "ready_provider",
                "object_id": fallback_row["object_id"] if fallback_row else None,
                "durability": "relay_backed" if fallback_row else "provider_bound",
                "binding_generation": generation,
            },
        ) or row

    async def _store_fallback_logged(
        self,
        *,
        ingress_id: str,
        row: dict[str, Any],
        data: bytes,
        retention_policy: str,
        provider: str | None,
        connection_id: str | None,
    ) -> dict[str, Any]:
        started_ms = now_ms()
        log_info(
            logger,
            "material_fallback_store_started",
            ingress_id=ingress_id,
            material_id=row.get("id"),
            provider=provider,
            connection_id=connection_id,
            retention_policy=retention_policy,
            size_bytes=len(data),
        )
        try:
            result = await self.fallback.store(
                material_id=row["id"],
                tenant_id=row["tenant_id"],
                conversation_hash=row["conversation_hash"],
                filename=row["filename"],
                content_type=row["content_type"],
                data=data,
                retention_policy=retention_policy,
            )
        except Exception as exc:
            log_error(
                logger,
                "material_fallback_store_failed",
                exc_info=True,
                ingress_id=ingress_id,
                material_id=row.get("id"),
                provider=provider,
                connection_id=connection_id,
                retention_policy=retention_policy,
                duration_ms=elapsed_ms(started_ms),
                failure_class="dependency",
                dependency="fallback_storage",
                exception_type=type(exc).__name__,
            )
            raise
        log_info(
            logger,
            "material_fallback_store_completed",
            ingress_id=ingress_id,
            material_id=row.get("id"),
            provider=provider,
            connection_id=connection_id,
            retention_policy=retention_policy,
            storage_id=result.get("storage_id"),
            duration_ms=elapsed_ms(started_ms),
        )
        return result

    async def _archive_attempt_payload(
        self,
        material: dict[str, Any],
        attempt_id: str,
        data: bytes | None,
        content_type: str,
        filename: str,
    ) -> str | None:
        if data is None:
            return None
        object_id = f"obj_{uuid4().hex}"
        path = relay_object_path(
            self.settings,
            material["tenant_id"],
            material["conversation_hash"],
            object_id,
            f"{attempt_id}-{filename}",
        )
        backend = self.fallback.storage.get(self.settings.default_storage_id)
        location = await backend.put_bytes(path, data, content_type=content_type.split(";", 1)[0])
        await self.repo.create_object(
            {
                "id": object_id,
                "tenant_id": material["tenant_id"],
                "conversation_hash": material["conversation_hash"],
                "storage_id": location.storage_id,
                "bucket": location.bucket,
                "object_key": location.key,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "content_type": content_type,
                "created_at": utcnow().isoformat(),
            }
        )
        return object_id

    async def _safe_create_attempt(self, row: dict[str, Any], *, ingress_id: str) -> None:
        try:
            await self.repo.create_binding_attempt(row)
        except Exception as exc:
            log_warning(
                logger,
                "material_binding_attempt_audit_failed",
                ingress_id=ingress_id,
                material_id=row.get("material_id"),
                attempt_id=row.get("attempt_id"),
                phase="create",
                exception_type=type(exc).__name__,
            )

    async def _safe_update_attempt(
        self,
        attempt_id: str,
        values: dict[str, Any],
        *,
        ingress_id: str,
        material_id: str,
    ) -> None:
        try:
            await self.repo.update_binding_attempt(attempt_id, values)
        except Exception as exc:
            log_warning(
                logger,
                "material_binding_attempt_audit_failed",
                ingress_id=ingress_id,
                material_id=material_id,
                attempt_id=attempt_id,
                phase="update",
                exception_type=type(exc).__name__,
            )

    @staticmethod
    def _fallback_eligible(exc: BaseException) -> bool:
        if isinstance(exc, ProviderHTTPError):
            return exc.status_code in {408, 425} or exc.status_code >= 500
        if isinstance(exc, (httpx.TransportError, TimeoutError)):
            return True
        if isinstance(exc, ProviderRequestError):
            return exc.code in {"PROVIDER_FILE_PROCESSING_TIMEOUT"}
        return False

    @staticmethod
    def _is_uncertain(exc: BaseException) -> bool:
        return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))

    @staticmethod
    def _error_detail(exc: BaseException) -> dict[str, Any]:
        if isinstance(exc, ProviderHTTPError):
            inline = raw_body_inline_fields(
                exc.body,
                content_type=exc.content_type,
                content_encoding=exc.content_encoding,
            )
            return {
                "source": "provider_file_api",
                "upstream_http_status": exc.status_code,
                "upstream_request_id": exc.request_id,
                "upstream_headers": dict(exc.response_headers),
                "content_type": exc.content_type,
                "content_encoding": exc.content_encoding,
                "body_size": len(exc.body),
                "body_sha256": hashlib.sha256(exc.body).hexdigest(),
                **inline,
                "phase": exc.phase,
                "exception_type": type(exc).__name__,
                "message": inline.get("body_text") or str(exc),
            }
        if isinstance(exc, ProviderRequestError):
            return {
                "source": "relay",
                "code": exc.code,
                "exception_type": type(exc).__name__,
                "message": exc.message,
            }
        return {
            "source": "relay",
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
            "cause_message": str(exc.__cause__) if exc.__cause__ else None,
        }

    @staticmethod
    def _source_error_detail(exc: BaseException) -> dict[str, Any]:
        if isinstance(exc, MaterialFetchError):
            detail: dict[str, Any] = {
                "source": "material_source",
                "code": exc.code,
                "phase": exc.phase,
                "upstream_http_status": exc.status_code,
                "upstream_headers": dict(exc.response_headers),
                "bytes_received": exc.bytes_received,
                "exception_type": type(exc).__name__,
                "message": str(exc),
            }
            if exc.body is not None:
                detail.update(
                    {
                        "body_size": len(exc.body),
                        "body_sha256": hashlib.sha256(exc.body).hexdigest(),
                        **raw_body_inline_fields(
                            exc.body,
                            content_type=exc.response_headers.get("content-type"),
                            content_encoding=exc.response_headers.get("content-encoding"),
                        ),
                    }
                )
            return detail
        return {
            "source": "material_source",
            "code": "MATERIAL_SOURCE_FETCH_FAILED",
            "phase": "source_fetch",
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
            "cause_message": str(exc.__cause__) if exc.__cause__ else None,
        }

    @staticmethod
    def _source_failure_class(exc: BaseException) -> str:
        if isinstance(exc, MaterialFetchError):
            if exc.code in {"MATERIAL_SOURCE_POLICY_REJECTED", "MATERIAL_SOURCE_TOO_LARGE"}:
                return "client"
            if exc.code == "MATERIAL_SOURCE_TIMEOUT":
                return "source_timeout"
            if exc.code in {"MATERIAL_SOURCE_TRANSPORT_ERROR", "MATERIAL_SOURCE_DNS_FAILED"}:
                return "source_transport"
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                return "source_rejected"
            return "source"
        return "source"

    @staticmethod
    def _sanitized_source_url(value: str | None) -> str | None:
        """Return only source origin for metadata/logging.

        Temporary Dify/provider URLs can contain bearer-like path segments as
        well as query signatures. The original URL is transport input, not a
        durable material fact, so ordinary logs and material metadata retain
        only scheme + host (+ explicit port).
        """
        if not value:
            return None
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, "", "", ""))
