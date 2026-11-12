from __future__ import annotations

import logging
from uuid import uuid4


from app.core.idempotency import stable_hash
from app.runtime.affinity import session_affinity_token
from app.execution_plane.supply_resolver import context_settings
from app.observability import info as log_info, warning as log_warning
from app.protocol_adapters.wire_projection.gemini_physical import GEMINI_SESSION_PROJECTION_METADATA_KEY, frozen_session_projection_metadata_v9
from app.execution_plane.route_resolver import RouteResolutionError
from app.utils import stable_prompt_cache_key
from app.v2_models import SessionCreateRequest


from app.execution_plane.services import ExecutionRequestError

logger = logging.getLogger("model-relay-execution-plane")

from app.execution_plane.acceptance_support import _validate_materials_for_route

class SessionManager:
    def __init__(self, services):
        self.services = services

    async def create(self, body: SessionCreateRequest, idempotency_key: str, *, _excluded=None):
        services = self.services
        repo = services.repo
        settings = services.settings
        caller_version = services.headers.get("x-relay-client-version")
        session_intent = {
            "tenant_id": body.tenant_id,
            "conversation_hash": body.conversation_hash,
            "provider": body.provider,
            "model": body.model,
            "context_policy": body.context_policy if isinstance(body.context_policy, str) else body.context_policy.model_dump(mode="json"),
            "material_ids": body.material_ids,
            "execution_pool": body.execution_pool or settings.execution_pool,
            "capability_requirements": body.capability_requirements.model_dump(mode="json"),
            "metadata": body.metadata,
        }
        session_hash = stable_hash(session_intent)
        legacy_session_hash = stable_hash(body.model_dump(mode="json"))
        existing = await repo.find_session_by_idempotency(body.tenant_id, idempotency_key)
        if existing:
            if existing.get("session_hash") not in {session_hash, legacy_session_hash}:
                raise ExecutionRequestError(status_code=409, detail="Idempotency-Key was already used for a different Session")
            log_info(
                logger,
                "session_idempotent_reuse",
                session_id=existing.get("id"),
                provider=existing.get("provider"),
                connection_id=existing.get("connection_id"),
                model=existing.get("model"),
                execution_pool=existing.get("execution_pool"),
            )
            return existing

        try:
            from app.execution_plane.admission import qualify_candidates
            failures=await qualify_candidates(services,body)
            failures.update(_excluded or {})
            route, route_group, route_pin_state = services.route_resolver.resolve_on_session_create(body,qualification_failures=failures)
            if ((route.capability_contract or {}).get("thinking") or {}).get("version") != "relay-thinking/2":
                raise ExecutionRequestError(409, {"code": "THINKING_CONTRACT_UPGRADE_REQUIRED"})
            services.route_resolver.handle_legacy_hint(
                client_hint=body.connection_id,
                resolved=route,
                caller_version=caller_version,
                scope="session",
            )
        except RouteResolutionError as exc:
            raise ExecutionRequestError(status_code=409, detail=exc.public_detail()) from exc

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
            raise ExecutionRequestError(
                status_code=409,
                detail={
                    "code": "EXECUTION_POOL_MISMATCH",
                    "requested_pool": body.execution_pool,
                    "local_pool": route.execution_pool,
                    "deployment_id": settings.deployment_id,
                },
            )

        await _validate_materials_for_route(
            services,
            material_ids=body.material_ids,
            tenant_id=body.tenant_id,
            conversation_hash=body.conversation_hash,
            route=route,
        )

        if any(str(key).startswith("_relay_") for key in body.metadata):
            raise ExecutionRequestError(status_code=422, detail={"code": "SESSION_INTERNAL_METADATA_FORBIDDEN"})
        session_id = uuid4()
        session_metadata = dict(body.metadata)
        session_metadata["_relay_route"] = route.internal_metadata()
        session_metadata["_relay_material_transport"] = {
            "version": "aggregate-files-threshold/1",
            "threshold_bytes": int(settings.gemini_files_threshold_bytes),
            "threshold_inclusive": True,
        }
        material_transport = (route.capability_contract or {}).get("metadata", {}).get("material_transport")
        if material_transport:
            session_metadata["_relay_material_transport"] = {"version": "supply-material-transport/1", **material_transport}
        if settings.material_placement_mode != "legacy_v1":
            session_metadata["_relay_material_placement"] = {"contract_version": "material-placement/2"}
        context = context_settings(body.context_policy)
        session_metadata["_relay_cache_v66"] = context
        if route_group:
            session_metadata["_relay_route_group"] = route_group
        if route.provider == "gemini":
            # Physical provider layout is frozen at Session creation. Existing
            # Sessions created before this version keep their prior wire semantics.
            session_metadata[GEMINI_SESSION_PROJECTION_METADATA_KEY] = frozen_session_projection_metadata_v9()
        affinity_token = session_affinity_token(settings, {
            "tenant_id": body.tenant_id, "id": str(session_id), "connection_id": route.connection_id,
        }, 1)
        from app.execution_plane.admission import reservation_scope
        if route.quota.get('max_active_sessions') or route.quota.get('budget_limit_units'):
            session_metadata['_relay_admission']={'scope':reservation_scope(services.route_resolver.config_snapshot,route.connection_id)}
        row_data = (
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
                "context_policy": context["context_policy"],
                "history_mode": context["history_mode"],
                "sticky_scope": context["sticky_scope"],
                "route_pin_state": route_pin_state,
                "supply_group_id": route.supply_group_id,
                "material_manifest": body.material_ids,
                "active_request_id": None,
                "execution_pool": route.execution_pool,
                "protocol_version": "v3.0",
                "session_affinity_token": affinity_token,
                "idempotency_key": idempotency_key,
                "session_hash": session_hash,
                "metadata": session_metadata,
                "expires_at": repo.default_session_expiry().isoformat(),
            }
        )
        try:
            row=await repo.create_session(row_data)
        except Exception as exc:
            from app.v2_repository import AdmissionUnavailable
            if not isinstance(exc,AdmissionUnavailable): raise
            excluded={**(_excluded or {}),route.offering_id:exc.reason}
            return await self.create(body,idempotency_key,_excluded=excluded)
        if str(row['id']) != str(session_id):
            log_info(logger,'session_idempotent_reuse',session_id=row['id'],connection_id=row['connection_id'])
            return row
        log_info(
            logger,
            "session_route_frozen",
            session_id=row.get("id"),
            provider=route.provider,
            model=route.model,
            route_revision=route.route_revision,
            route_binding_hash=route.route_binding_hash,
            qualification_failures=failures,
            connection_id=route.connection_id,
            adapter_version=route.inference_adapter_version,
            file_adapter_version=route.file_adapter_version,
            capability_revision=route.capability_revision,
            offering_id=route.offering_id,
            channel_id=route.channel_id,
            protocol=route.protocol,
            capability_contract_id=route.capability_contract_id,
            control_plane_hash=route.control_plane_hash,
            execution_pool=route.execution_pool,
            gemini_physical_layout=(
                session_metadata.get(GEMINI_SESSION_PROJECTION_METADATA_KEY, {}).get("layout_version")
                if route.provider == "gemini"
                else None
            ),
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
        return row

