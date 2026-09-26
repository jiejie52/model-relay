from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from ..providers.base import ProviderRequestError
from ..observability import error as log_error, info as log_info, warning as log_warning
from ..utils import safe_segment, utcnow
from ..v2_repository import RelayV2Repository
from .fallback_storage import FallbackObjectStorage
from .gemini_transport import (
    GEMINI_EXTERNAL_URL_REPRESENTATION,
    GEMINI_FILES_REPRESENTATION,
    GeminiTransportDecision,
    decide_gemini_request_transport,
    external_url_binding,
    project_gemini_external_url_filename,
    project_gemini_input_content_type,
)
from .provider_files.base import MaterialFile
from .provider_files.registry import ProviderFileRegistry


logger = logging.getLogger("model-relay-material-bindings")


class BindingResolver:
    def __init__(
        self,
        repo: RelayV2Repository,
        fallback: FallbackObjectStorage,
        file_adapters: ProviderFileRegistry,
    ) -> None:
        self.repo = repo
        self.fallback = fallback
        self.file_adapters = file_adapters

    async def freeze_for_request(
        self,
        *,
        material_ids: list[str],
        connection_id: str,
        tenant_id: str,
        conversation_hash: str,
        existing_snapshot: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # Once a Request has frozen bindings, retries reuse those exact provider
        # identities instead of silently switching generations. The frozen
        # snapshot must still belong to the Session's private route.
        adapter = self.file_adapters.maybe_get(connection_id)
        if existing_snapshot:
            for item in existing_snapshot:
                frozen_connection = str(item.get("connection_id") or "")
                if frozen_connection != connection_id:
                    log_error(
                        logger,
                        "route_binding_mismatch",
                        request_id=request_id,
                        session_id=session_id,
                        material_id=item.get("material_id"),
                        expected_connection_id=connection_id,
                        frozen_connection_id=frozen_connection,
                        failure_class="relay_validation",
                    )
                    raise ProviderRequestError(
                        "ROUTE_BINDING_MISMATCH",
                        "Frozen material binding does not match the Session route",
                    )
                if adapter is not None:
                    frozen_scope = str(item.get("account_scope_hash") or "")
                    if frozen_scope != adapter.account_scope_hash:
                        log_error(
                            logger,
                            "route_binding_mismatch",
                            request_id=request_id,
                            session_id=session_id,
                            material_id=item.get("material_id"),
                            connection_id=connection_id,
                            expected_account_scope_hash=adapter.account_scope_hash,
                            frozen_account_scope_hash=frozen_scope,
                            failure_class="relay_validation",
                        )
                        raise ProviderRequestError(
                            "ROUTE_BINDING_MISMATCH",
                            "Frozen material binding belongs to a different Provider account scope",
                        )
            return [dict(item) for item in existing_snapshot]

        materials: list[dict[str, Any]] = []
        for material_id in material_ids:
            material = await self.repo.get_material(
                material_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
            )
            if not material:
                raise ProviderRequestError("MATERIAL_NOT_FOUND", f"Material not found: {material_id}")
            if material.get("status") in {"failed", "deleted", "reupload_required"}:
                raise ProviderRequestError(
                    "MATERIAL_REUPLOAD_REQUIRED",
                    f"Material is not usable and must be uploaded again: {material_id}",
                )
            materials.append(material)

        gemini_decision: GeminiTransportDecision | None = None
        if adapter is not None and str(getattr(adapter, "provider", "") or "").lower() == "gemini":
            sizes: list[int] = []
            for material in materials:
                raw_size = material.get("actual_size")
                if raw_size is None:
                    raw_size = material.get("size_bytes")
                if raw_size is None:
                    raise ProviderRequestError(
                        "MATERIAL_SIZE_MISSING",
                        f"Relay has no authoritative byte size for material {material.get('id')}",
                    )
                sizes.append(int(raw_size))
            gemini_decision = decide_gemini_request_transport(
                material_sizes=sizes,
                threshold_bytes=int(getattr(self.fallback.settings, "gemini_files_threshold_bytes", 99 * 1024 * 1024)),
            )
            log_info(
                logger,
                "gemini_request_size_authoritative",
                request_id=request_id,
                session_id=session_id,
                connection_id=connection_id,
                material_count=len(materials),
                material_total_bytes=gemini_decision.total_bytes,
                threshold_bytes=gemini_decision.threshold_bytes,
                selected_transport=gemini_decision.mode,
                decision_source=gemini_decision.source,
            )

        snapshots: list[dict[str, Any]] = []
        for material in materials:
            material_id = str(material["id"])

            if adapter is None:
                fallback = await self.repo.get_material_fallback(material_id)
                if not fallback:
                    raise ProviderRequestError(
                        "MATERIAL_REUPLOAD_REQUIRED",
                        f"Connection {connection_id} requires Relay fallback bytes but material {material_id} has none",
                    )
                snapshots.append(
                    {
                        "material_id": material_id,
                        "binding_generation": int(material.get("binding_generation") or 0),
                        "binding_kind": "fallback_object",
                        "connection_id": connection_id,
                        "account_scope_hash": "relay-fallback",
                        "content_sha256": material.get("sha256"),
                        "content_type": material.get("content_type"),
                        "filename": material.get("filename"),
                        "object_id": fallback.get("object_id"),
                    }
                )
                continue

            binding = await self.repo.get_provider_binding(
                material_id=material_id,
                connection_id=connection_id,
                account_scope_hash=adapter.account_scope_hash,
            )

            if gemini_decision is not None:
                binding = await self._ensure_gemini_request_binding(
                    material=material,
                    binding=binding,
                    adapter=adapter,
                    connection_id=connection_id,
                    tenant_id=tenant_id,
                    conversation_hash=conversation_hash,
                    decision=gemini_decision,
                    request_id=request_id,
                    session_id=session_id,
                )
            elif not self._binding_usable(binding):
                binding = await self._refresh_generic_binding(
                    material=material,
                    binding=binding,
                    adapter=adapter,
                    connection_id=connection_id,
                    tenant_id=tenant_id,
                    conversation_hash=conversation_hash,
                )

            snapshots.append(self._snapshot(material, binding))
        return snapshots

    async def _ensure_gemini_request_binding(
        self,
        *,
        material: dict[str, Any],
        binding: dict[str, Any] | None,
        adapter: Any,
        connection_id: str,
        tenant_id: str,
        conversation_hash: str,
        decision: GeminiTransportDecision,
        request_id: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        material_id = str(material["id"])
        desired_representation = (
            GEMINI_EXTERNAL_URL_REPRESENTATION
            if decision.mode == "supabase_external_url"
            else GEMINI_FILES_REPRESENTATION
        )
        if self._binding_usable(binding) and str(binding.get("representation") or "") == desired_representation:
            # If a Files binding already exists, remove any stale external-URL
            # input copy left by an earlier per-material decision.
            if desired_representation == GEMINI_FILES_REPRESENTATION:
                await self._cleanup_gemini_supabase_copy(
                    material=material,
                    request_id=request_id,
                    session_id=session_id,
                    decision=decision,
                )
                return binding

            # 0.5.10 projected only fileData.mimeType.  Existing External URL
            # bindings may therefore still point to a Supabase object stored as
            # application/json/.json.  Reuse only bindings whose backing bridge
            # object already matches the provider-facing projection.
            existing_fallback = await self.repo.get_material_fallback(material_id)
            if existing_fallback and await self._gemini_external_projection_matches(
                material=material,
                fallback=existing_fallback,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
            ):
                return binding

        fallback = await self.repo.get_material_fallback(material_id)
        generation = int((binding or {}).get("generation") or material.get("binding_generation") or 0) + 1

        if desired_representation == GEMINI_EXTERNAL_URL_REPRESENTATION:
            if not fallback:
                # 0.5.2 could create a small material through Files API when the
                # caller omitted an aggregate. Relay cannot reconstruct the
                # original bytes from Gemini fileUri, so force a clean reupload
                # rather than silently keep the wrong transport.
                await self.repo.update_material(
                    material_id,
                    {"status": "reupload_required", "durability": "reupload_required"},
                )
                raise ProviderRequestError(
                    "MATERIAL_REUPLOAD_REQUIRED",
                    f"Material {material_id} must be reuploaded so Relay can create the <=99 MiB Supabase External URL binding",
                )
            fallback = await self._ensure_gemini_external_projection(
                material=material,
                fallback=fallback,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                request_id=request_id,
                session_id=session_id,
            )
            ttl_seconds = max(300, min(int(self.fallback.settings.supabase_signed_url_ttl), 604800))
            signed_url = await self.fallback.sign_read_url(fallback, expires_in=ttl_seconds)
            binding = external_url_binding(
                material_id=material_id,
                connection_id=connection_id,
                account_scope_hash=adapter.account_scope_hash,
                external_url=signed_url,
                object_id=str(fallback["object_id"]),
                generation=generation,
                ttl_seconds=ttl_seconds,
                metadata={
                    "transport": "supabase_external_url",
                    "storage_id": fallback.get("storage_id"),
                    "object_id": fallback.get("object_id"),
                    "authoritative_request_total_bytes": decision.total_bytes,
                    "threshold_bytes": decision.threshold_bytes,
                    "request_reconciled": True,
                    "source_filename": material.get("filename"),
                    "source_content_type": material.get("content_type"),
                    "projected_filename": project_gemini_external_url_filename(
                        material.get("filename"), material.get("content_type")
                    ),
                    "projected_content_type": project_gemini_input_content_type(
                        material.get("content_type")
                    ),
                    "projection_revision": "gemini-external-url-projection/1",
                },
            )
            binding.setdefault("created_at", utcnow().isoformat())
            binding["updated_at"] = utcnow().isoformat()
            await self.repo.upsert_provider_binding(binding)
            await self._update_gemini_material_transport(
                material,
                decision=decision,
                generation=generation,
                durability="relay_backed",
            )
            log_info(
                logger,
                "gemini_request_transport_reconciled",
                request_id=request_id,
                session_id=session_id,
                material_id=material_id,
                selected_transport=decision.mode,
                binding_generation=generation,
                material_total_bytes=decision.total_bytes,
            )
            return binding

        if not fallback:
            if self._binding_usable(binding) and str(binding.get("representation") or "") == GEMINI_FILES_REPRESENTATION:
                return binding
            await self.repo.update_material(
                material_id,
                {"status": "reupload_required", "durability": "reupload_required"},
            )
            raise ProviderRequestError(
                "MATERIAL_REUPLOAD_REQUIRED",
                f"Material {material_id} has no Relay bytes available for Gemini Files API promotion",
            )

        log_info(
            logger,
            "gemini_request_transport_promotion_started",
            request_id=request_id,
            session_id=session_id,
            material_id=material_id,
            from_representation=(binding or {}).get("representation"),
            to_representation=GEMINI_FILES_REPRESENTATION,
            material_total_bytes=decision.total_bytes,
            threshold_bytes=decision.threshold_bytes,
        )
        data = await self.fallback.read(fallback)
        result = await adapter.prepare(
            MaterialFile(
                material_id=material_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                filename=str(material.get("filename") or material_id),
                content_type=project_gemini_input_content_type(material.get("content_type")),
                size_bytes=len(data),
                sha256=str(material.get("sha256") or ""),
                data=data,
            ),
            generation=generation,
        )
        binding = dict(result.binding)
        binding["material_id"] = material_id
        binding.setdefault("created_at", utcnow().isoformat())
        binding["updated_at"] = utcnow().isoformat()
        metadata = dict(binding.get("metadata") or {})
        metadata.update(
            {
                "transport": "gemini_files",
                "authoritative_request_total_bytes": decision.total_bytes,
                "threshold_bytes": decision.threshold_bytes,
                "request_reconciled": True,
            }
        )
        binding["metadata"] = metadata
        await self.repo.upsert_provider_binding(binding)
        await self._update_gemini_material_transport(
            material,
            decision=decision,
            generation=generation,
            durability="relay_backed",
        )
        await self._cleanup_gemini_supabase_copy(
            material=material,
            request_id=request_id,
            session_id=session_id,
            decision=decision,
        )
        log_info(
            logger,
            "gemini_request_transport_promotion_completed",
            request_id=request_id,
            session_id=session_id,
            material_id=material_id,
            selected_transport=decision.mode,
            binding_generation=generation,
            material_total_bytes=decision.total_bytes,
        )
        return binding

    async def _gemini_external_projection_matches(
        self,
        *,
        material: dict[str, Any],
        fallback: dict[str, Any],
        tenant_id: str,
        conversation_hash: str,
    ) -> bool:
        object_id = str(fallback.get("object_id") or "")
        if not object_id:
            return False
        obj = await self.repo.get_object(
            object_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        if not obj:
            return False
        expected_type = project_gemini_input_content_type(material.get("content_type"))
        expected_name = project_gemini_external_url_filename(
            material.get("filename"), material.get("content_type")
        )
        current_type = str(obj.get("content_type") or "").split(";", 1)[0].strip().lower()
        expected_type_norm = str(expected_type or "").split(";", 1)[0].strip().lower()
        current_name = str(fallback.get("object_key") or "").rsplit("/", 1)[-1]
        return current_type == expected_type_norm and current_name == safe_segment(expected_name)

    async def _ensure_gemini_external_projection(
        self,
        *,
        material: dict[str, Any],
        fallback: dict[str, Any],
        tenant_id: str,
        conversation_hash: str,
        request_id: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        if await self._gemini_external_projection_matches(
            material=material,
            fallback=fallback,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        ):
            return fallback

        material_id = str(material["id"])
        data = await self.fallback.read(fallback)
        projected_type = project_gemini_input_content_type(material.get("content_type"))
        projected_name = project_gemini_external_url_filename(
            material.get("filename"), material.get("content_type")
        )
        previous = dict(fallback)
        replacement = await self.fallback.store(
            material_id=material_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            filename=projected_name,
            content_type=projected_type,
            data=data,
            retention_policy="gemini-external-url-bridge",
        )
        await self.repo.update_material(material_id, {"object_id": replacement.get("object_id")})
        try:
            await self.fallback.delete_object_only(previous)
        except Exception as exc:
            # The new bridge is already authoritative.  Failure to remove the
            # superseded object is visible but must not invalidate the binding.
            log_warning(
                logger,
                "gemini_external_url_projection_cleanup_failed",
                request_id=request_id,
                session_id=session_id,
                material_id=material_id,
                object_id=previous.get("object_id"),
                exception_type=type(exc).__name__,
                failure_class="dependency",
                reason=str(exc),
            )
        log_info(
            logger,
            "gemini_external_url_projection_rebuilt",
            request_id=request_id,
            session_id=session_id,
            material_id=material_id,
            source_filename=material.get("filename"),
            source_content_type=material.get("content_type"),
            projected_filename=projected_name,
            projected_content_type=projected_type,
            previous_object_id=previous.get("object_id"),
            object_id=replacement.get("object_id"),
        )
        return replacement

    async def _cleanup_gemini_supabase_copy(
        self,
        *,
        material: dict[str, Any],
        request_id: str | None,
        session_id: str | None,
        decision: GeminiTransportDecision,
    ) -> None:
        material_id = str(material["id"])
        fallback = await self.repo.get_material_fallback(material_id)
        if not fallback:
            await self.repo.update_material(material_id, {"durability": "provider_bound", "object_id": None})
            return
        try:
            await self.fallback.delete(fallback)
            await self.repo.update_material(
                material_id,
                {"durability": "provider_bound", "object_id": None},
            )
            log_info(
                logger,
                "gemini_supabase_input_copy_removed",
                request_id=request_id,
                session_id=session_id,
                material_id=material_id,
                object_id=fallback.get("object_id"),
                material_total_bytes=decision.total_bytes,
                reason="authoritative request total exceeds Gemini External URL threshold",
            )
        except Exception as exc:
            # The provider binding is already ready. Cleanup failure must be
            # visible but must not duplicate model inference or file upload.
            log_warning(
                logger,
                "gemini_supabase_input_copy_cleanup_failed",
                request_id=request_id,
                session_id=session_id,
                material_id=material_id,
                object_id=fallback.get("object_id"),
                exception_type=type(exc).__name__,
                failure_class="dependency",
                reason=str(exc),
            )

    async def _update_gemini_material_transport(
        self,
        material: dict[str, Any],
        *,
        decision: GeminiTransportDecision,
        generation: int,
        durability: str,
    ) -> None:
        metadata = dict(material.get("metadata") or {})
        policy = dict(metadata.get("_relay_gemini_transport") or {})
        policy.update(
            {
                "mode": decision.mode,
                "decision_source": decision.source,
                "authoritative_request_total_bytes": decision.total_bytes,
                "threshold_bytes": decision.threshold_bytes,
            }
        )
        metadata["_relay_gemini_transport"] = policy
        await self.repo.update_material(
            str(material["id"]),
            {
                "status": "ready_provider",
                "binding_generation": generation,
                "durability": durability,
                "metadata": metadata,
            },
        )

    async def _refresh_generic_binding(
        self,
        *,
        material: dict[str, Any],
        binding: dict[str, Any] | None,
        adapter: Any,
        connection_id: str,
        tenant_id: str,
        conversation_hash: str,
    ) -> dict[str, Any]:
        material_id = str(material["id"])
        fallback = await self.repo.get_material_fallback(material_id)
        if not fallback:
            await self.repo.update_material(
                material_id,
                {"status": "reupload_required", "durability": "reupload_required"},
            )
            raise ProviderRequestError(
                "MATERIAL_REUPLOAD_REQUIRED",
                f"Provider binding is unavailable and no fallback bytes exist: {material_id}",
            )

        generation = int((binding or {}).get("generation") or material.get("binding_generation") or 0) + 1
        if self._uses_gemini_external_url(material, binding, adapter):
            fallback = await self._ensure_gemini_external_projection(
                material=material,
                fallback=fallback,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                request_id=None,
                session_id=None,
            )
            ttl_seconds = max(300, min(int(self.fallback.settings.supabase_signed_url_ttl), 604800))
            signed_url = await self.fallback.sign_read_url(fallback, expires_in=ttl_seconds)
            binding = external_url_binding(
                material_id=material_id,
                connection_id=connection_id,
                account_scope_hash=adapter.account_scope_hash,
                external_url=signed_url,
                object_id=str(fallback["object_id"]),
                generation=generation,
                ttl_seconds=ttl_seconds,
                metadata={
                    "transport": "supabase_external_url",
                    "storage_id": fallback.get("storage_id"),
                    "object_id": fallback.get("object_id"),
                    "refresh": True,
                },
            )
        else:
            data = await self.fallback.read(fallback)
            result = await adapter.prepare(
                MaterialFile(
                    material_id=material_id,
                    tenant_id=tenant_id,
                    conversation_hash=conversation_hash,
                    filename=str(material.get("filename") or material_id),
                    content_type=(
                        project_gemini_input_content_type(material.get("content_type"))
                        if str(getattr(adapter, "provider", "") or "").lower() == "gemini"
                        else str(material.get("content_type") or "application/octet-stream")
                    ),
                    size_bytes=len(data),
                    sha256=str(material.get("sha256") or ""),
                    data=data,
                ),
                generation=generation,
            )
            binding = dict(result.binding)
            binding["material_id"] = material_id

        binding.setdefault("created_at", utcnow().isoformat())
        binding["updated_at"] = utcnow().isoformat()
        await self.repo.upsert_provider_binding(binding)
        await self.repo.update_material(
            material_id,
            {
                "status": "ready_provider",
                "binding_generation": generation,
                "durability": "relay_backed",
            },
        )
        return binding

    @staticmethod
    def _uses_gemini_external_url(
        material: dict[str, Any],
        binding: dict[str, Any] | None,
        adapter: Any,
    ) -> bool:
        if str(getattr(adapter, "provider", "") or "").lower() != "gemini":
            return False
        if binding and str(binding.get("representation") or "") == GEMINI_EXTERNAL_URL_REPRESENTATION:
            return True
        metadata = material.get("metadata") if isinstance(material.get("metadata"), dict) else {}
        policy = metadata.get("_relay_gemini_transport") if isinstance(metadata.get("_relay_gemini_transport"), dict) else {}
        return str(policy.get("mode") or "") == "supabase_external_url"

    @staticmethod
    def _binding_usable(binding: dict[str, Any] | None) -> bool:
        if not binding:
            return False
        state = str(binding.get("state") or binding.get("processing_state") or "").lower()
        if state not in {"active", "ready", "processed"}:
            return False
        expires = binding.get("expires_at")
        if not expires:
            return True
        try:
            dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt > utcnow()
        except Exception:
            return False

    @staticmethod
    def _snapshot(material: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
        return {
            "material_id": material["id"],
            "binding_generation": int(binding.get("generation") or 1),
            "binding_kind": binding.get("representation") or binding.get("purpose") or "provider_file",
            "connection_id": binding.get("connection_id"),
            "account_scope_hash": binding.get("account_scope_hash"),
            "provider": binding.get("provider"),
            "purpose": binding.get("purpose"),
            "representation": binding.get("representation"),
            "external_file_id": binding.get("external_file_id") or binding.get("provider_file_id"),
            "external_uri": binding.get("external_uri") or binding.get("file_uri"),
            "content_sha256": material.get("sha256"),
            "content_type": (
                project_gemini_input_content_type(material.get("content_type"))
                if str(binding.get("provider") or "").lower() == "gemini"
                else material.get("content_type")
            ),
            "filename": material.get("filename"),
            "metadata": binding.get("metadata") or {},
        }
