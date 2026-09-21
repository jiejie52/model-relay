from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from ..config import Settings
from ..core.raw_error import raw_body_inline_fields
from ..providers.base import ProviderHTTPError, ProviderRequestError
from ..storage_paths import relay_object_path
from ..utils import utcnow
from ..observability import elapsed_ms, error as log_error, info as log_info, warning as log_warning, now_ms, exception_failure_class
from ..v2_repository import RelayV2Repository
from .fallback_storage import FallbackObjectStorage
from .provider_files.base import MaterialFile, ProviderFileResult
from .provider_files.registry import ProviderFileRegistry
from .safe_fetch import fetch_bytes


logger = logging.getLogger("model-relay-materials")


class MaterialIngressError(RuntimeError):
    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__(str(detail.get("message") or detail.get("code") or "material ingress failed"))
        self.detail = detail


class MaterialIngress:
    """Provider-native-first material ingress.

    A stable relay_materials row is created before provider upload. Gemini/Kimi
    native success does not create an input-file object in Supabase. Supabase is
    used for input bytes only when fallback/bridge/durability policy requires it.
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
        target_connection_id: str | None = None,
        durability_policy: str | None = None,
        fallback_policy: str | None = None,
        declared_size: int | None = None,
    ) -> dict[str, Any]:
        existing = await self.repo.find_material_by_idempotency(tenant_id, idempotency_key)
        if existing:
            log_info(
                logger,
                "material_ingress_idempotent_reuse",
                material_id=existing.get("id"),
                target_connection_id=existing.get("target_connection_id"),
                status=existing.get("status"),
            )
            return existing

        durability_policy = (durability_policy or self.settings.material_default_durability_policy).lower()
        fallback_policy = (fallback_policy or self.settings.material_default_fallback_policy).lower()
        if durability_policy not in {"native_first", "relay_backed"}:
            raise ValueError("durability_policy must be native_first or relay_backed")
        if fallback_policy not in {"never", "on_provider_unavailable", "always"}:
            raise ValueError("fallback_policy must be never, on_provider_unavailable or always")

        detected_content_type = content_type
        source_kind = "inline"
        if source_url is not None:
            source_kind = "readable_url"
            data, fetched_content_type = await fetch_bytes(
                source_url,
                max_bytes=self.settings.material_ingress_max_bytes,
                timeout_seconds=self.settings.material_ingress_timeout_seconds,
                allow_http=self.settings.material_allow_http,
            )
            detected_content_type = detected_content_type or fetched_content_type
        if data is None:
            raise ValueError("material data is required")
        if len(data) > self.settings.material_ingress_max_bytes:
            raise ValueError("material exceeds configured ingress size limit")
        if declared_size is not None and int(declared_size) != len(data):
            raise ValueError(
                f"declared_size does not match received bytes: declared={declared_size} actual={len(data)}"
            )

        detected_content_type = (detected_content_type or "application/octet-stream").split(";", 1)[0].strip()
        digest = hashlib.sha256(data).hexdigest()
        material_id = f"mat_{uuid4().hex}"
        ingress_started_ms = now_ms()
        row = await self.repo.create_material(
            {
                "id": material_id,
                "tenant_id": tenant_id,
                "conversation_hash": conversation_hash,
                "status": "binding" if target_connection_id else "receiving",
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
                # A signed/temporary URL is not treated as a durable rebind source.
                "source_recoverable": False,
                "target_connection_id": target_connection_id,
                "durability_policy": durability_policy,
                "fallback_policy": fallback_policy,
                "durability": "provider_bound" if target_connection_id else "relay_backed",
                "parent_material_id": parent_material_id,
                "ordinal": ordinal,
                "metadata": metadata or {},
                "created_at": utcnow().isoformat(),
            }
        )

        log_info(
            logger,
            "material_ingress_started",
            material_id=material_id,
            filename=filename,
            content_type=detected_content_type,
            size_bytes=len(data),
            target_connection_id=target_connection_id,
            durability_policy=durability_policy,
            fallback_policy=fallback_policy,
            source_kind=source_kind,
        )

        adapter = self.file_adapters.maybe_get(target_connection_id)
        fallback_row: dict[str, Any] | None = None

        # Backward-compatible/generic path: without a native file adapter the
        # material must be retained as a transport bridge (e.g. Grok signed URL).
        if adapter is None:
            fallback_row = await self._store_fallback(
                row=row,
                data=data,
                retention_policy="bridge" if target_connection_id else "legacy-default",
            )
            log_info(
                logger,
                "material_fallback_stored",
                material_id=material_id,
                target_connection_id=target_connection_id,
                storage_id=fallback_row.get("storage_id"),
                retention_policy="bridge" if target_connection_id else "legacy-default",
                duration_ms=elapsed_ms(ingress_started_ms),
            )
            return await self.repo.update_material(
                material_id,
                {
                    "status": "ready",
                    "object_id": fallback_row["object_id"],
                    "durability": "relay_backed",
                },
            ) or row

        generation = 1
        attempt_id = f"mba_{uuid4().hex}"
        await self._safe_create_attempt(
            {
                "attempt_id": attempt_id,
                "material_id": material_id,
                "connection_id": target_connection_id,
                "account_scope_hash": adapter.account_scope_hash,
                "generation": generation,
                "phase": "started",
                "uncertain": False,
                "detail": {"provider": adapter.provider, "adapter_version": adapter.adapter_version},
                "created_at": utcnow().isoformat(),
            }
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
            material_id=material_id,
            provider=adapter.provider,
            connection_id=target_connection_id,
            adapter_version=adapter.adapter_version,
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
            )
            log_info(
                logger,
                "provider_file_binding_completed",
                material_id=material_id,
                provider=adapter.provider,
                connection_id=target_connection_id,
                adapter_version=adapter.adapter_version,
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
                material_id=material_id,
                provider=adapter.provider,
                connection_id=target_connection_id,
                adapter_version=adapter.adapter_version,
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
            )
            if fallback_policy in {"on_provider_unavailable", "always"} and self._fallback_eligible(exc):
                fallback_row = await self._store_fallback(
                    row=row,
                    data=data,
                    retention_policy="provider-unavailable",
                )
                log_warning(
                    logger,
                    "material_provider_fallback_activated",
                    material_id=material_id,
                    provider=adapter.provider,
                    connection_id=target_connection_id,
                    failure_class=exception_failure_class(exc),
                    upstream_http_status=getattr(exc, "status_code", None),
                    retention_policy="provider-unavailable",
                    duration_ms=elapsed_ms(ingress_started_ms),
                )
                return await self.repo.update_material(
                    material_id,
                    {
                        "status": "fallback_stored",
                        "object_id": fallback_row["object_id"],
                        "durability": "relay_backed",
                        "metadata": {**(metadata or {}), "last_binding_error": raw_error},
                    },
                ) or row
            await self.repo.update_material(
                material_id,
                {"status": "failed", "metadata": {**(metadata or {}), "last_binding_error": raw_error}},
            )
            raise MaterialIngressError(raw_error) from exc

        if durability_policy == "relay_backed" or fallback_policy == "always":
            fallback_row = await self._store_fallback(
                row=row,
                data=data,
                retention_policy="durability" if durability_policy == "relay_backed" else "policy-always",
            )

        log_info(
            logger,
            "material_ingress_completed",
            material_id=material_id,
            provider=adapter.provider,
            connection_id=target_connection_id,
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

    async def _store_fallback(
        self,
        *,
        row: dict[str, Any],
        data: bytes,
        retention_policy: str,
    ) -> dict[str, Any]:
        return await self.fallback.store(
            material_id=row["id"],
            tenant_id=row["tenant_id"],
            conversation_hash=row["conversation_hash"],
            filename=row["filename"],
            content_type=row["content_type"],
            data=data,
            retention_policy=retention_policy,
        )

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

    async def _safe_create_attempt(self, row: dict[str, Any]) -> None:
        try:
            await self.repo.create_binding_attempt(row)
        except Exception:
            # Attempt telemetry must not prevent file delivery. The provider/raw
            # error path remains authoritative even if audit insert is unavailable.
            pass

    async def _safe_update_attempt(self, attempt_id: str, values: dict[str, Any]) -> None:
        try:
            await self.repo.update_binding_attempt(attempt_id, values)
        except Exception:
            pass

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
    def _sanitized_source_url(value: str | None) -> str | None:
        if not value:
            return None
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
