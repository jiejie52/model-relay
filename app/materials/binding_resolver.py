from __future__ import annotations

from datetime import datetime, timezone
import base64
import hashlib
import logging
from typing import Any

from ..providers.base import ProviderRequestError
from ..observability import error as log_error, info as log_info, warning as log_warning
from ..utils import safe_segment, utcnow
from ..v2_repository import RelayV2Repository
from .fallback_storage import FallbackObjectStorage
from .gemini_cache_projection import (
    GEMINI_CACHE_PROJECTION_VERSION,
    GEMINI_INLINE_CACHE_REPRESENTATION,
    plan_gemini_cache_materials,
    plan_gemini_cache_materials_legacy,
    select_gemini_inline_cache_material_ids,
)
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
        gemini_inline_cache_strategy: bool = False,
        gemini_session_material_ids: list[str] | None = None,
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
        gemini_inline_cache_ids: set[str] = set()
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
                threshold_bytes=int(getattr(self.fallback.settings, "gemini_cache_inline_fallback_limit_bytes", 70 * 1024 * 1024)),
            )
            if gemini_inline_cache_strategy:
                gemini_inline_cache_ids = set(
                    select_gemini_inline_cache_material_ids(
                        material_rows=materials,
                        session_material_ids=gemini_session_material_ids or [],
                        inline_limit_bytes=gemini_decision.threshold_bytes,
                    )
                )
            else:
                # Layout/2-5 Sessions are frozen to the historical Files-first
                # request behavior even after 4.3 changes the new-session policy.
                gemini_decision = GeminiTransportDecision(
                    mode="gemini_files",
                    total_bytes=gemini_decision.total_bytes,
                    threshold_bytes=gemini_decision.threshold_bytes,
                    source="gemini_files_preferred_legacy_layout",
                )
            log_info(
                logger,
                "gemini_request_size_authoritative",
                request_id=request_id,
                session_id=session_id,
                connection_id=connection_id,
                material_count=len(materials),
                material_total_bytes=gemini_decision.total_bytes,
                cache_inline_fallback_limit_bytes=gemini_decision.threshold_bytes,
                selected_transport=gemini_decision.mode,
                cache_inline_material_count=len(gemini_inline_cache_ids),
                files_api_inference_count=(
                    0
                    if gemini_decision.total_bytes < gemini_decision.threshold_bytes
                    else len([m for m in materials if str(m.get("id") or "") not in gemini_inline_cache_ids])
                ),
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
                if gemini_inline_cache_strategy:
                    binding = await self._ensure_gemini_split_request_binding(
                        material=material,
                        binding=binding,
                        adapter=adapter,
                        connection_id=connection_id,
                        tenant_id=tenant_id,
                        conversation_hash=conversation_hash,
                        decision=gemini_decision,
                        cache_inline=(material_id in gemini_inline_cache_ids),
                        request_id=request_id,
                        session_id=session_id,
                    )
                else:
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

    async def prepare_gemini_cache_projection(
        self,
        *,
        material_ids: list[str],
        material_bindings: list[dict[str, Any]],
        session: dict[str, Any],
        tenant_id: str,
        conversation_hash: str,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Build the cache-only Gemini material projection.

        Layout/6+ never puts Gemini File references into CachedContent. Stable
        cache-selected material is read from Relay storage and injected as
        inlineData. Layout/3-5 keep their frozen historical projection semantics.
        """

        rows: list[dict[str, Any]] = []
        for material_id in material_ids:
            row = await self.repo.get_material(
                str(material_id),
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
            )
            if not row:
                raise ProviderRequestError("MATERIAL_NOT_FOUND", f"Material not found: {material_id}")
            rows.append(row)

        session_material_ids = [
            str(x)
            for x in (session.get("material_manifest") or [])
            if str(x)
        ] if session.get("context_policy") == "conversation" else []
        inline_limit = int(
            getattr(
                self.fallback.settings,
                "gemini_cache_inline_fallback_limit_bytes",
                70 * 1024 * 1024,
            )
        )
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        projection_meta = metadata.get("_relay_gemini_projection") if isinstance(metadata.get("_relay_gemini_projection"), dict) else {}
        layout_version = str(projection_meta.get("layout_version") or "")
        is_inline_cache_layout = layout_version in {
            "gemini-physical-cache-layout/6",
            "gemini-physical-cache-layout/7",
        }

        try:
            if is_inline_cache_layout:
                planned_obj = plan_gemini_cache_materials(
                    material_rows=rows,
                    material_bindings=material_bindings,
                    session_material_ids=session_material_ids,
                    inline_limit_bytes=inline_limit,
                )
                canonical = planned_obj.canonical()
            else:
                canonical = plan_gemini_cache_materials_legacy(
                    material_rows=rows,
                    material_bindings=material_bindings,
                    session_material_ids=session_material_ids,
                    inline_limit_bytes=inline_limit,
                )
        except ValueError as exc:
            raise ProviderRequestError("GEMINI_CACHE_MATERIAL_PLAN_INVALID", str(exc)) from exc

        row_by_id = {str(row["id"]): row for row in rows}
        binding_by_id = {str(binding.get("material_id") or ""): binding for binding in material_bindings}
        inline_ids = [str(x) for x in (canonical.get("inline_material_ids") or []) if str(x)]
        inline_set = set(inline_ids)
        planned_cache_ids = [str(x) for x in (canonical.get("cache_material_ids") or []) if str(x)]
        cache_bindings: list[dict[str, Any]] = []
        actual_cache_ids: list[str] = []
        missing_inline_ids: list[str] = []

        for material_id in planned_cache_ids:
            binding = binding_by_id.get(material_id)
            row = row_by_id.get(material_id)
            if binding is None or row is None:
                continue

            if material_id not in inline_set:
                # Legacy layout/3-5 may cache an existing Gemini File binding.
                cache_bindings.append(dict(binding))
                actual_cache_ids.append(material_id)
                continue

            fallback = await self.repo.get_material_fallback(material_id)
            if not fallback:
                missing_inline_ids.append(material_id)
                continue
            data = await self.fallback.read(fallback)
            expected_sha = str(row.get("sha256") or "")
            actual_sha = hashlib.sha256(data).hexdigest()
            if expected_sha and actual_sha != expected_sha:
                raise ProviderRequestError(
                    "MATERIAL_INTEGRITY_MISMATCH",
                    f"Relay fallback bytes do not match canonical Material {material_id}",
                )
            cache_bindings.append(
                {
                    "material_id": material_id,
                    "binding_generation": int(binding.get("binding_generation") or binding.get("generation") or 1),
                    "binding_kind": GEMINI_INLINE_CACHE_REPRESENTATION,
                    "representation": GEMINI_INLINE_CACHE_REPRESENTATION,
                    "connection_id": binding.get("connection_id"),
                    "account_scope_hash": binding.get("account_scope_hash"),
                    "provider": "gemini",
                    "purpose": "cache_inline_static",
                    "content_sha256": expected_sha or actual_sha,
                    "content_type": project_gemini_input_content_type(row.get("content_type")),
                    "filename": row.get("filename"),
                    "inline_data": base64.b64encode(data).decode("ascii"),
                    "metadata": {
                        "projection_version": GEMINI_CACHE_PROJECTION_VERSION,
                        "source_representation": binding.get("representation"),
                        "source_object_id": fallback.get("object_id"),
                        "size_bytes": len(data),
                        "single_point_static_injection": True,
                    },
                }
            )
            actual_cache_ids.append(material_id)

        actual_cache_set = set(actual_cache_ids)
        inference_only = [
            material_id
            for material_id in session_material_ids
            if material_id not in actual_cache_set
        ]
        canonical.update(
            {
                "cache_material_ids": actual_cache_ids,
                "inference_only_session_material_ids": inference_only,
                "missing_inline_material_ids": missing_inline_ids,
                "cache_material_bindings": cache_bindings,
            }
        )
        log_info(
            logger,
            "gemini_cache_material_projection_prepared",
            request_id=request_id,
            session_id=session_id,
            layout_version=layout_version,
            material_total_bytes=canonical.get("total_material_bytes"),
            inline_limit_bytes=canonical.get("inline_limit_bytes"),
            mode=canonical.get("mode"),
            inline_cache_count=len(actual_cache_ids),
            files_api_cache_count=len(canonical.get("files_api_material_ids") or canonical.get("files_api_cache_material_ids") or []),
            files_api_inference_count=len(canonical.get("files_api_inference_material_ids") or []),
            external_url_inference_count=len(canonical.get("external_url_inference_material_ids") or []),
            inference_only_session_count=len(inference_only),
            missing_inline_count=len(missing_inline_ids),
        )
        return canonical

    async def _ensure_gemini_split_request_binding(
        self,
        *,
        material: dict[str, Any],
        binding: dict[str, Any] | None,
        adapter: Any,
        connection_id: str,
        tenant_id: str,
        conversation_hash: str,
        decision: GeminiTransportDecision,
        cache_inline: bool,
        request_id: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        """Resolve Relay 4.3 Gemini material placement.

        Below 70 MiB no Files API side effect is permitted. At/above 70 MiB the
        deterministic small stable subset is kept in Relay storage for inline
        CachedContent projection; every remaining material uses Gemini Files API
        first and falls back to a signed External URL on upload failure.
        """

        material_id = str(material["id"])
        representation = str((binding or {}).get("representation") or "")
        metadata = binding.get("metadata") if isinstance((binding or {}).get("metadata"), dict) else {}
        under_limit = decision.total_bytes < decision.threshold_bytes
        use_files = (not under_limit) and (not cache_inline)

        if not use_files:
            if self._binding_usable(binding) and representation == GEMINI_EXTERNAL_URL_REPRESENTATION:
                return binding
            fallback = await self.repo.get_material_fallback(material_id)
            if not fallback:
                await self.repo.update_material(
                    material_id,
                    {"status": "reupload_required", "durability": "reupload_required"},
                )
                raise ProviderRequestError(
                    "MATERIAL_REUPLOAD_REQUIRED",
                    f"Material {material_id} has no Relay bytes for Gemini inline-cache strategy",
                )
            fallback = await self._ensure_gemini_external_projection(
                material=material,
                fallback=fallback,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                request_id=request_id,
                session_id=session_id,
            )
            generation = int((binding or {}).get("generation") or material.get("binding_generation") or 0) + 1
            ttl_seconds = max(300, min(int(self.fallback.settings.supabase_signed_url_ttl), 604800))
            signed_url = await self.fallback.sign_read_url(fallback, expires_in=ttl_seconds)
            external = external_url_binding(
                material_id=material_id,
                connection_id=connection_id,
                account_scope_hash=adapter.account_scope_hash,
                external_url=signed_url,
                object_id=str(fallback["object_id"]),
                generation=generation,
                ttl_seconds=ttl_seconds,
                metadata={
                    "transport": "supabase_external_url",
                    "relay43_strategy": decision.mode,
                    "files_api_skipped": True,
                    "cache_inline_selected": bool(cache_inline),
                    "cache_projection": "inline_static" if cache_inline else "inference_only",
                    "storage_id": fallback.get("storage_id"),
                    "object_id": fallback.get("object_id"),
                    "authoritative_request_total_bytes": decision.total_bytes,
                    "cache_inline_limit_bytes": decision.threshold_bytes,
                },
            )
            external.setdefault("created_at", utcnow().isoformat())
            external["updated_at"] = utcnow().isoformat()
            await self.repo.upsert_provider_binding(external)
            await self._update_gemini_material_transport(
                material,
                decision=decision,
                generation=generation,
                durability="relay_backed",
            )
            log_info(
                logger,
                "gemini_request_material_external_url_bound",
                request_id=request_id,
                session_id=session_id,
                material_id=material_id,
                connection_id=connection_id,
                request_strategy=decision.mode,
                cache_inline_selected=bool(cache_inline),
                files_api_attempted=False,
                material_total_bytes=decision.total_bytes,
                cache_inline_limit_bytes=decision.threshold_bytes,
            )
            return external

        # >=70 MiB and not selected for the stable inline cache subset: use
        # Gemini Files API for inference only. A previously frozen Files-failure
        # External URL is reused so retries do not repeat the provider side effect.
        if self._binding_usable(binding) and representation == GEMINI_FILES_REPRESENTATION:
            return binding
        if (
            self._binding_usable(binding)
            and representation == GEMINI_EXTERNAL_URL_REPRESENTATION
            and bool(metadata.get("files_api_fallback"))
        ):
            return binding

        fallback = await self.repo.get_material_fallback(material_id)
        if not fallback:
            await self.repo.update_material(
                material_id,
                {"status": "reupload_required", "durability": "reupload_required"},
            )
            raise ProviderRequestError(
                "MATERIAL_REUPLOAD_REQUIRED",
                f"Material {material_id} has no Relay bytes for Gemini Files upload",
            )

        generation = int((binding or {}).get("generation") or material.get("binding_generation") or 0) + 1
        data = await self.fallback.read(fallback)
        log_info(
            logger,
            "gemini_files_inference_upload_started",
            request_id=request_id,
            session_id=session_id,
            material_id=material_id,
            connection_id=connection_id,
            request_strategy=decision.mode,
            cache_inline_selected=False,
            material_size_bytes=len(data),
            material_total_bytes=decision.total_bytes,
        )
        try:
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
        except Exception as exc:
            log_warning(
                logger,
                "gemini_files_inference_upload_failed",
                request_id=request_id,
                session_id=session_id,
                material_id=material_id,
                connection_id=connection_id,
                request_strategy=decision.mode,
                exception_type=type(exc).__name__,
                failure_class="dependency",
                upstream_http_status=getattr(exc, "status_code", None),
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
            external = external_url_binding(
                material_id=material_id,
                connection_id=connection_id,
                account_scope_hash=adapter.account_scope_hash,
                external_url=signed_url,
                object_id=str(fallback["object_id"]),
                generation=generation,
                ttl_seconds=ttl_seconds,
                metadata={
                    "transport": "supabase_external_url",
                    "relay43_strategy": decision.mode,
                    "files_api_fallback": True,
                    "cache_inline_selected": False,
                    "cache_projection": "inference_only",
                    "storage_id": fallback.get("storage_id"),
                    "object_id": fallback.get("object_id"),
                    "authoritative_request_total_bytes": decision.total_bytes,
                    "cache_inline_limit_bytes": decision.threshold_bytes,
                },
            )
            external.setdefault("created_at", utcnow().isoformat())
            external["updated_at"] = utcnow().isoformat()
            await self.repo.upsert_provider_binding(external)
            await self._update_gemini_material_transport(
                material,
                decision=decision,
                generation=generation,
                durability="relay_backed",
            )
            log_warning(
                logger,
                "gemini_files_inference_external_url_fallback_activated",
                request_id=request_id,
                session_id=session_id,
                material_id=material_id,
                connection_id=connection_id,
                request_strategy=decision.mode,
                cache_projection="inference_only",
            )
            return external

        file_binding = dict(result.binding)
        file_binding["material_id"] = material_id
        file_binding.setdefault("created_at", utcnow().isoformat())
        file_binding["updated_at"] = utcnow().isoformat()
        file_metadata = dict(file_binding.get("metadata") or {})
        file_metadata.update(
            {
                "transport": "gemini_files",
                "relay43_strategy": decision.mode,
                "cache_inline_selected": False,
                "cache_projection": "inference_only",
                "authoritative_request_total_bytes": decision.total_bytes,
                "cache_inline_limit_bytes": decision.threshold_bytes,
            }
        )
        file_binding["metadata"] = file_metadata
        await self.repo.upsert_provider_binding(file_binding)
        # Keep Relay fallback bytes for deterministic re-upload/fallback after a
        # provider File expires; Files is an inference transport, not durability.
        await self._update_gemini_material_transport(
            material,
            decision=decision,
            generation=generation,
            durability="relay_backed",
        )
        log_info(
            logger,
            "gemini_files_inference_upload_completed",
            request_id=request_id,
            session_id=session_id,
            material_id=material_id,
            connection_id=connection_id,
            request_strategy=decision.mode,
            binding_generation=generation,
            material_total_bytes=decision.total_bytes,
        )
        return file_binding

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
        """Resolve the inference binding with Gemini Files as the only primary.

        A previously frozen ``files_api_fallback`` External URL is reused instead
        of retrying the provider-file side effect on every request.  Legacy
        External URL bindings are promoted once when Relay still has canonical
        bytes; if that Files upload fails, the failure is frozen as an External
        URL inference fallback and the same bytes remain available for the
        layout/3 cache-only inline projection.
        """

        material_id = str(material["id"])
        representation = str((binding or {}).get("representation") or "")
        binding_metadata = (
            binding.get("metadata")
            if isinstance((binding or {}).get("metadata"), dict)
            else {}
        )

        if (
            self._binding_usable(binding)
            and representation == GEMINI_EXTERNAL_URL_REPRESENTATION
            and bool(binding_metadata.get("files_api_fallback"))
        ):
            # Files API has already been attempted for this binding generation.
            # Reusing the frozen fallback prevents a new provider-file side
            # effect on every inference turn.
            return binding

        if self._binding_usable(binding) and representation == GEMINI_FILES_REPRESENTATION:
            # Files is authoritative. Remove an obsolete Supabase bridge left by
            # an older transport decision; cleanup failure does not invalidate
            # the usable Provider binding.
            await self._cleanup_gemini_supabase_copy(
                material=material,
                request_id=request_id,
                session_id=session_id,
                decision=decision,
            )
            return binding

        fallback = await self.repo.get_material_fallback(material_id)
        generation = int((binding or {}).get("generation") or material.get("binding_generation") or 0) + 1
        if not fallback:
            await self.repo.update_material(
                material_id,
                {"status": "reupload_required", "durability": "reupload_required"},
            )
            raise ProviderRequestError(
                "MATERIAL_REUPLOAD_REQUIRED",
                f"Material {material_id} has no Relay bytes available for Gemini Files API upload",
            )

        log_info(
            logger,
            "gemini_request_transport_promotion_started",
            request_id=request_id,
            session_id=session_id,
            material_id=material_id,
            from_representation=representation or None,
            to_representation=GEMINI_FILES_REPRESENTATION,
            material_total_bytes=decision.total_bytes,
            cache_inline_fallback_limit_bytes=decision.threshold_bytes,
        )
        data = await self.fallback.read(fallback)
        try:
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
        except Exception as exc:
            # 4.0: Files remains the primary. Supabase is only the frozen
            # inference fallback after Files upload failure; CachedContent never
            # receives this External URL. The cache projection may instead use
            # the same canonical bytes as inlineData according to the 70 MiB
            # aggregate rule.
            log_warning(
                logger,
                "gemini_files_preferred_binding_failed",
                request_id=request_id,
                session_id=session_id,
                material_id=material_id,
                connection_id=connection_id,
                exception_type=type(exc).__name__,
                failure_class="dependency",
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
            external = external_url_binding(
                material_id=material_id,
                connection_id=connection_id,
                account_scope_hash=adapter.account_scope_hash,
                external_url=signed_url,
                object_id=str(fallback["object_id"]),
                generation=generation,
                ttl_seconds=ttl_seconds,
                metadata={
                    "transport": "supabase_external_url",
                    "files_api_fallback": True,
                    "cache_projection": "inline_candidate",
                    "storage_id": fallback.get("storage_id"),
                    "object_id": fallback.get("object_id"),
                    "authoritative_request_total_bytes": decision.total_bytes,
                    "cache_inline_fallback_limit_bytes": decision.threshold_bytes,
                    "request_reconciled": True,
                    "source_filename": material.get("filename"),
                    "source_content_type": material.get("content_type"),
                    "projected_filename": project_gemini_external_url_filename(
                        material.get("filename"), material.get("content_type")
                    ),
                    "projected_content_type": project_gemini_input_content_type(
                        material.get("content_type")
                    ),
                    "projection_revision": "gemini-external-url-projection/2",
                },
            )
            external.setdefault("created_at", utcnow().isoformat())
            external["updated_at"] = utcnow().isoformat()
            await self.repo.upsert_provider_binding(external)

            metadata = dict(material.get("metadata") or {})
            policy = dict(metadata.get("_relay_gemini_transport") or {})
            policy.update(
                {
                    "mode": "gemini_files_primary_external_url_fallback",
                    "decision_source": "gemini_files_failure",
                    "files_api_failed": True,
                    "cache_inline_candidate": True,
                    "authoritative_request_total_bytes": decision.total_bytes,
                    "cache_inline_fallback_limit_bytes": decision.threshold_bytes,
                }
            )
            metadata["_relay_gemini_transport"] = policy
            await self.repo.update_material(
                material_id,
                {
                    "status": "ready_provider",
                    "object_id": fallback.get("object_id"),
                    "durability": "relay_backed",
                    "binding_generation": generation,
                    "metadata": metadata,
                },
            )
            log_warning(
                logger,
                "gemini_files_fallback_external_url_activated",
                request_id=request_id,
                session_id=session_id,
                material_id=material_id,
                connection_id=connection_id,
                representation=GEMINI_EXTERNAL_URL_REPRESENTATION,
                cache_projection="inline_candidate",
                material_total_bytes=decision.total_bytes,
            )
            return external

        binding = dict(result.binding)
        binding["material_id"] = material_id
        binding.setdefault("created_at", utcnow().isoformat())
        binding["updated_at"] = utcnow().isoformat()
        metadata = dict(binding.get("metadata") or {})
        metadata.update(
            {
                "transport": "gemini_files",
                "authoritative_request_total_bytes": decision.total_bytes,
                "cache_inline_fallback_limit_bytes": decision.threshold_bytes,
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
            selected_transport=GEMINI_FILES_REPRESENTATION,
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
                reason="Gemini Files binding is authoritative; obsolete Supabase inference bridge removed",
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
                "cache_inline_fallback_limit_bytes": decision.threshold_bytes,
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
