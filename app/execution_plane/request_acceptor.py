from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4


from app.core.idempotency import caller_intent_hash, request_identity, stable_hash
from app.execution_plane.request_identity import semantic_intent_hash
from app.control_plane.execution_authorization import execution_authorization
from app.persistence.execution_artifacts import ExecutionArtifacts
from app.runtime.affinity import session_affinity_token
from app.execution_plane.cache_intent_planner import freeze_dual_track_plan
from app.core.context_codec import canonical_structure, normalize_structured_output, freeze_history
from app.execution_plane.context_planner import build_context_plan
from app.execution_plane.cache_intent_resolver import CacheIntentResolver
from app.shared_contracts import CacheDecisionError
from app.runtime.cache_economics import reuse_evidence_key, stable_attachment_evidence
from app.execution_plane.semantic_resolver import CAPABILITY_PROFILE_REVISION, ModelOptionError, resolve_model_options, validate_structured_output_capability
from app.observability import error as log_error, info as log_info, warning as log_warning
from app.persistence.object_storage import ObjectLocation
from app.execution_plane.route_resolver import RouteBinding, RouteResolutionError
from app.supabase import SupabaseError
from app.v2_models import SessionRequestCreate


from app.execution_plane.services import ExecutionRequestError
from app.execution_plane.structured_output import resolve_structured_output, StructuredOutputError

logger = logging.getLogger("model-relay-execution-plane")

from app.execution_plane.acceptance_support import _store_request_json_artifact, _validate_materials_for_route

class RequestAcceptor:
    def __init__(self, services):
        self.services = services

    async def accept(self, session_id: UUID, body: SessionRequestCreate, idempotency_key: str, owner: tuple[str, str]):
        thinking_field_present = "think_level" in body.model_fields_set
        services = self.services
        tenant_id, conversation_hash = owner
        repo = services.repo
        session = await repo.get_session(
            session_id, tenant_id=tenant_id, conversation_hash=conversation_hash
        )
        if not session:
            raise ExecutionRequestError(status_code=404, detail="Session not found")

        settings = services.settings
        if str(session.get("execution_pool") or "") != settings.execution_pool:
            raise ExecutionRequestError(
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
        caller_version = services.headers.get("x-relay-client-version")
        session_protocol = str(session.get("protocol_version") or "v2.1").lower()
        cache_contract_session = session_protocol.startswith("v3")
        cache_field_explicit = (
            "requested_cache_mode" in body.model_fields_set
            or "requested_cache_mechanism" in body.model_fields_set
        )
        cache_mechanism_explicit = "requested_cache_mechanism" in body.model_fields_set
        if not cache_contract_session and cache_field_explicit:
            raise ExecutionRequestError(
                status_code=409,
                detail={
                    "code": "CACHE_CONTRACT_UPGRADE_REQUIRED",
                    "message": "Create a new Session before requesting cache controls",
                },
            )

        if "_relay_audit" in body.metadata:
            raise ExecutionRequestError(422, {"code": "AUDIT_CONTEXT_OVERRIDE_FORBIDDEN"})

        protected_metadata = [
            str(key) for key in body.metadata if str(key).startswith("_relay_cache")
        ]
        if protected_metadata:
            raise ExecutionRequestError(
                status_code=422,
                detail={
                    "code": "CACHE_CONTROL_OVERRIDE_FORBIDDEN",
                    "message": "Caller metadata cannot override Relay cache-control fields",
                    "fields": protected_metadata,
                },
            )
        if cache_contract_session:
            native_cache_keys = {
                "prompt_cache_key",
                "cache_control",
                "cachedContent",
                "cached_content",
                "cache_ttl",
                "ttl",
            }
            attempted = sorted(native_cache_keys.intersection(body.provider_payload))
            if attempted:
                raise ExecutionRequestError(
                    status_code=422,
                    detail={
                        "code": "CACHE_CONTROL_OVERRIDE_FORBIDDEN",
                        "message": "Provider-native cache controls are server-governed",
                        "fields": attempted,
                    },
                )

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
                raise ExecutionRequestError(
                    status_code=409,
                    detail={
                        "code": "LEGACY_CONNECTION_HINT_MISMATCH",
                        "message": "Request route hints do not match the frozen Session route",
                    },
                )

        policy = ((session.get("metadata") or {}).get("_relay_route") or {}).get("cache_policy") or {}
        governed = policy.get("metadata") or {}
        if cache_contract_session and "requested_cache_mode" not in body.model_fields_set and governed.get("default_mode"):
            body = body.model_copy(update={"requested_cache_mode": governed["default_mode"]})
        if cache_contract_session and body.requested_cache_mode == "on" and governed.get("allow_force") is False:
            raise ExecutionRequestError(422, {"code": "CACHE_FORCE_NOT_AUTHORIZED"})
        requested_cache_mode = body.requested_cache_mode if cache_contract_session else None
        public_intent = {
            "input": body.input,
            "instructions": body.instructions,
            "material_ids": body.material_ids,
            "provider": provider,
            "model": model,
            "think_level": body.think_level,
            "execution": body.execution.model_dump(mode="json"),
            "structured_output": body.structured_output,
            "options": body.options,
            # provider_payload remains a public migration input. Even though v3
            # only accepts aliases that can be normalized into canonical options,
            # changing it under the same Idempotency-Key must not silently reuse an
            # earlier Request.
            "provider_payload": body.provider_payload,
            "requested_cache_mode": requested_cache_mode,
            "metadata": body.metadata,
        }
        if cache_contract_session and cache_mechanism_explicit:
            public_intent["requested_cache_mechanism"] = body.requested_cache_mechanism
            public_intent["requested_cache_mechanism_explicit"] = True
        public_intent_hash = caller_intent_hash(public_intent)

        # Cache-aware Requests must replay their original interpretation before any
        # current cache inventory, token measurement or policy is consulted.
        existing = await repo.find_request_by_idempotency(session_id, idempotency_key)
        if existing and existing.get("request_identity_version") == "relay-request/2.4":
            frozen = await ExecutionArtifacts(repo, services.storage, settings).load_json(
                existing["request_object_id"], tenant_id=tenant_id, conversation_hash=conversation_hash)
            try:
                replay_options = resolve_model_options(provider=frozen["provider"], model=frozen["model"],
                    options=body.options, provider_payload=body.provider_payload, think_level=body.think_level,
                    capability_contract=frozen.get("capability_contract"),
                    field_present=(thinking_field_present if "thinking" in frozen else None),
                    think_level_policy=body.think_level_policy)
                replay_hash = semantic_intent_hash(public_intent, replay_options)
            except (ModelOptionError, ValueError) as exc:
                raise ExecutionRequestError(409, {"code": "IDEMPOTENCY_CONFLICT"}) from exc
            if replay_hash != existing.get("caller_intent_hash"):
                raise ExecutionRequestError(409, {"code": "IDEMPOTENCY_CONFLICT"})
            if existing.get("execution_mode") == "sync" and existing.get("status") == "running" and existing.get("provider_dispatch_state") in {"not_sent", "result_stored"}:
                return await services.inline_scheduler.execute(existing)
            return existing
        if existing and str(existing.get("request_identity_version") or "") == "relay-request/2.3":
            if body.think_level_policy != "prefer":
                raise ExecutionRequestError(409, {"code": "IDEMPOTENCY_CONFLICT"})
            if str(existing.get("caller_intent_hash") or "") != public_intent_hash:
                raise ExecutionRequestError(status_code=409, detail="IDEMPOTENCY_CONFLICT")
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
                identity_version="relay-request/2.3",
            )
            if (existing.get("provider_dispatch_state") in {"not_sent", "result_stored"}
                    and existing.get("execution_mode") == "sync"):
                return await services.inline_scheduler.execute(existing)
            return existing

        if existing:
            # Older, non-cache Requests must be compared using their own stored
            # algorithm; never add a new thinking node to an accepted identity.
            frozen = await ExecutionArtifacts(repo, services.storage, settings).load_json(
                existing["request_object_id"], tenant_id=tenant_id, conversation_hash=conversation_hash)
            try:
                replay_options = resolve_model_options(provider=frozen["provider"], model=frozen["model"],
                    options=body.options, provider_payload=body.provider_payload, think_level=body.think_level,
                    capability_contract=frozen.get("capability_contract"),
                    field_present=(thinking_field_present if "thinking" in frozen else None),
                    think_level_policy=body.think_level_policy)
                replay = {**frozen, "input": body.input, "instructions": body.instructions,
                    "material_ids": body.material_ids, "think_level": replay_options.effective_think_level,
                    "options": replay_options.requested_options, "effective_options": replay_options.effective_options,
                    "provider_payload": body.provider_payload, "structured_output": body.structured_output,
                    "execution": body.execution.model_dump(mode="json")}
                if replay_options.thinking is not None: replay["thinking"] = replay_options.thinking
                if request_identity(replay) != existing.get("request_hash"):
                    raise ValueError("request identity differs")
            except (ModelOptionError, ValueError) as exc:
                raise ExecutionRequestError(409, {"code": "IDEMPOTENCY_CONFLICT"}) from exc
            if existing.get("execution_mode") == "sync" and existing.get("provider_dispatch_state") in {"not_sent", "result_stored"}:
                return await services.inline_scheduler.execute(existing)
            return existing

        pending_pin = None
        session_metadata = session.get("metadata") or {}
        is_v66 = isinstance(session_metadata.get("_relay_cache_v66"), dict)
        if is_v66:
            try:
                selected_route = services.route_resolver.materialize_on_first_request(session, body)
            except RouteResolutionError as exc:
                raise ExecutionRequestError(status_code=409, detail=exc.public_detail()) from exc
            session = {**session, "connection_id": selected_route["connection_id"],
                       "metadata": {**session_metadata, "_relay_route": selected_route}}
            connection_id = str(session["connection_id"])
            effective_instructions = body.instructions
            if effective_instructions is None and session.get("history_mode") == "ephemeral_turn":
                effective_instructions = session.get("frozen_instructions")
            session["system_semantic_hash"] = stable_hash(effective_instructions or "")
            affinity_token = session_affinity_token(settings, session, int(session.get("affinity_epoch") or 1))
            session["session_affinity_token"] = affinity_token
            pending_pin = {
                "route": selected_route,
                "group_hash": (session_metadata.get("_relay_route_group") or {}).get("group_hash"),
                "affinity_token": affinity_token,
                "instructions": effective_instructions,
                "system_semantic_hash": session["system_semantic_hash"],
            }
        else:
            effective_instructions = body.instructions

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
                capability_revision=str(frozen_route.get("capability_revision") or CAPABILITY_PROFILE_REVISION),
                offering_id=(str(frozen_route.get("offering_id")) if frozen_route.get("offering_id") else None),
                channel_id=(str(frozen_route.get("channel_id")) if frozen_route.get("channel_id") else None),
                protocol=(str(frozen_route.get("protocol")) if frozen_route.get("protocol") else None),
                capability_contract_id=(str(frozen_route.get("capability_contract_id")) if frozen_route.get("capability_contract_id") else None),
                capability_contract_hash=(str(frozen_route.get("capability_contract_hash")) if frozen_route.get("capability_contract_hash") else None),
                capability_contract=(dict(frozen_route.get("capability_contract")) if isinstance(frozen_route.get("capability_contract"), dict) else None),
                protocol_profile_id=(str(frozen_route.get("protocol_profile_id")) if frozen_route.get("protocol_profile_id") else None),
                protocol_profile_hash=(str(frozen_route.get("protocol_profile_hash")) if frozen_route.get("protocol_profile_hash") else None),
                protocol_profile=(dict(frozen_route.get("protocol_profile")) if isinstance(frozen_route.get("protocol_profile"), dict) else None),
                cache_policy_id=(str(frozen_route.get("cache_policy_id")) if frozen_route.get("cache_policy_id") else None),
                cache_policy_hash=(str(frozen_route.get("cache_policy_hash")) if frozen_route.get("cache_policy_hash") else None),
                cache_policy=(dict(frozen_route.get("cache_policy")) if isinstance(frozen_route.get("cache_policy"), dict) else None),
                cache_contract_hash=(str(frozen_route.get("cache_contract_hash")) if frozen_route.get("cache_contract_hash") else None),
                cache_contract=(dict(frozen_route.get("cache_contract")) if isinstance(frozen_route.get("cache_contract"), dict) else None),
                control_plane_hash=(str(frozen_route.get("control_plane_hash")) if frozen_route.get("control_plane_hash") else None),
                capability_requirements=(dict(frozen_route.get("capability_requirements")) if isinstance(frozen_route.get("capability_requirements"), dict) else {}),
                observed_model_policy=str(frozen_route.get("observed_model_policy") or "audit"),
                connection_identity_hash=frozen_route.get("connection_identity_hash"),
                upstream_model=frozen_route.get("upstream_model"),
                quota=(dict(frozen_route.get("quota")) if isinstance(frozen_route.get("quota"), dict) else {}),
            )
            await _validate_materials_for_route(
                services,
                material_ids=effective_material_ids,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                route=session_route,
            )

        material_hashes: dict[str, str] = {}
        material_rows: list[dict[str, Any]] = []
        material_total_bytes = 0
        for material_id in effective_material_ids:
            material = await repo.get_material(
                material_id, tenant_id=tenant_id, conversation_hash=conversation_hash
            )
            if not material or material.get("status") in {"failed", "deleted", "reupload_required", "receiving", "binding"}:
                raise ExecutionRequestError(status_code=409, detail=f"Material is not usable: {material_id}")
            material_rows.append(material)
            material_hashes[material_id] = str(material.get("sha256") or "")
            raw_size = material.get("actual_size")
            if raw_size is None:
                raw_size = material.get("size_bytes")
            if raw_size is None:
                raise ExecutionRequestError(
                    status_code=409,
                    detail={"code": "MATERIAL_SIZE_MISSING", "material_id": material_id},
                )
            material_total_bytes += int(raw_size)

        if provider == "gemini":
            transport_policy = (session.get("metadata") or {}).get("_relay_material_transport") or {}
            inline_limit = int(transport_policy.get("threshold_bytes") or
                getattr(settings, "gemini_cache_inline_fallback_limit_bytes", 70 * 1024 * 1024))
            external_transport = (material_total_bytes <= inline_limit if transport_policy.get("threshold_inclusive")
                                  else material_total_bytes < inline_limit)
            log_info(
                logger,
                "gemini_request_size_calculated",
                session_id=str(session_id),
                provider=provider,
                model=model,
                connection_id=connection_id,
                material_count=len(effective_material_ids),
                material_total_bytes=material_total_bytes,
                cache_inline_fallback_limit_bytes=inline_limit,
                selected_transport=(
                    "inline_cache_no_files"
                    if external_transport
                    else "hybrid_inline_cache_files"
                ),
                cache_branch=(
                    "inline_all_stable_materials_no_files_api"
                    if external_transport
                    else "inline_small_stable_subset_files_for_remaining"
                ),
                decision_source="relay_request_material_sum_authoritative",
            )

        try:
            resolved_options = resolve_model_options(
                provider=provider,
                model=model,
                options=body.options,
                provider_payload=body.provider_payload,
                think_level=body.think_level,
                field_present=thinking_field_present,
                think_level_policy=body.think_level_policy,
                capability_contract=(
                    dict(frozen_route.get("capability_contract"))
                    if isinstance(frozen_route.get("capability_contract"), dict)
                    else None
                ),
            )
            structured_output_guarantee = validate_structured_output_capability(
                provider=provider,
                model=model,
                structured_output=body.structured_output,
                capability_contract=(
                    dict(frozen_route.get("capability_contract"))
                    if isinstance(frozen_route.get("capability_contract"), dict)
                    else None
                ),
            )
        except ModelOptionError as exc:
            log_warning(
                logger,
                "model_options_rejected",
                raw_think_level=body.think_level,
                think_level_source=("omitted" if not thinking_field_present else "explicit_empty" if body.think_level is None or not body.think_level.strip() else "caller_explicit"),
                think_level_policy=body.think_level_policy,
                entry_point=services.entry_point,
                frozen_contract_id=frozen_route.get("capability_contract_id"),
                session_id=str(session_id),
                provider=provider,
                model=model,
                code=exc.code,
                option=exc.option,
                capability_revision=exc.public_detail().get("capability_revision"),
                failure_class="client",
            )
            raise ExecutionRequestError(status_code=422, detail=exc.public_detail()) from exc

        if cache_contract_session:
            public_intent_hash = semantic_intent_hash(public_intent, resolved_options)

        frozen_capability_revision = str(frozen_route.get("capability_revision") or "")
        frozen_contract = frozen_route.get("capability_contract")
        if (
            not isinstance(frozen_contract, dict)
            and frozen_capability_revision
            and frozen_capability_revision != resolved_options.capability_revision
        ):
            raise ExecutionRequestError(
                status_code=409,
                detail={
                    "code": "CAPABILITY_PROFILE_CHANGED_RECREATE_SESSION",
                    "message": "The Session froze an older model-option capability profile; create a new Session before submitting a new Request",
                    "session_capability_revision": frozen_capability_revision,
                    "current_capability_revision": resolved_options.capability_revision,
                },
            )

        if resolved_options.thinking is not None:
            log_info(logger, "thinking_semantics_resolved", session_id=str(session_id),
                raw_think_level=body.think_level, think_level_source=resolved_options.thinking["source"],
                think_level_policy=body.think_level_policy, requested_intent=resolved_options.thinking["requested_intent"],
                effective_thinking=resolved_options.thinking["effective"], mapping_rule_id=resolved_options.thinking["mapping_rule_id"],
                mapping_reason=resolved_options.thinking["mapping_reason"], frozen_contract_id=frozen_route.get("capability_contract_id"),
                entry_point=services.entry_point)
        for warning in resolved_options.warnings:
            log_warning(
                logger,
                "model_options_compatibility_warning",
                session_id=str(session_id),
                provider=provider,
                model=model,
                warning=warning,
                capability_revision=resolved_options.capability_revision,
            )

        base_snapshot = {
            "audit_context": {"entry_point": services.entry_point},
            "protocol_profile": frozen_route.get("protocol_profile"),
            "execution_authorization": dict(execution_authorization(frozen_route.get("execution_authorization"))),
            "session_id": str(session_id),
            "tenant_id": tenant_id,
            "conversation_hash": conversation_hash,
            "input": body.input,
            "instructions": effective_instructions,
            "material_ids": body.material_ids,
            "effective_material_ids": effective_material_ids,
            "material_hashes": material_hashes,
            "material_total_bytes": material_total_bytes,
            "provider": provider,
            "connection_id": connection_id,
            "connection_identity_hash": frozen_route.get("connection_identity_hash"),
            **({k:frozen_route[k] for k in ('connection_contract_version','connection_spec_hash','account_scope_id')} if frozen_route.get('connection_contract_version')==2 else {}),
            "upstream_model": frozen_route.get("upstream_model"),
            "model": model,
            "offering_id": frozen_route.get("offering_id") if frozen_route else None,
            "channel_id": frozen_route.get("channel_id") if frozen_route else None,
            "protocol": frozen_route.get("protocol") if frozen_route else None,
            "control_plane_hash": frozen_route.get("control_plane_hash") if frozen_route else None,
            "route_revision": frozen_route.get("route_revision") if frozen_route else None,
            "route_binding_hash": frozen_route.get("route_binding_hash") if frozen_route else None,
            "capability_revision": resolved_options.capability_revision,
            "capability_contract_id": frozen_route.get("capability_contract_id") if frozen_route else None,
            "capability_contract_hash": frozen_route.get("capability_contract_hash") if frozen_route else None,
            "capability_contract": (dict(frozen_route.get("capability_contract")) if isinstance(frozen_route.get("capability_contract"), dict) else None),
            "observed_model_policy": str(frozen_route.get("observed_model_policy") or "audit") if frozen_route else "audit",
            **({"thinking": resolved_options.thinking} if resolved_options.thinking is not None else {}),
            "requested_think_level": resolved_options.requested_think_level,
            "think_level": resolved_options.effective_think_level,
            "execution": body.execution.model_dump(mode="json"),
            "structured_output": body.structured_output,
            "structured_output_guarantee": structured_output_guarantee,
            "options": resolved_options.requested_options,
            "effective_options": resolved_options.effective_options,
            "provider_payload": body.provider_payload,
            "metadata": body.metadata,
        }

        try:
            resolve_structured_output(base_snapshot, fallback_name="structured_output")
        except StructuredOutputError as exc:
            raise ExecutionRequestError(status_code=422, detail={"code": exc.code, "message": exc.message}) from exc
        context_plan: dict[str, Any] | None = None
        cache_plan: dict[str, Any] | None = None
        history_identity: dict[str, Any] | None = None
        planning_history = None
        if cache_contract_session:
            if session.get("history_mode", "cumulative") != "ephemeral_turn" and session.get("history_object_id"):
                history_obj = await repo.get_object(
                    str(session["history_object_id"]),
                    tenant_id=tenant_id,
                    conversation_hash=conversation_hash,
                )
                if is_v66 and not history_obj:
                    raise ExecutionRequestError(status_code=409, detail={"code": "HISTORY_INTEGRITY_MISMATCH"})
                if history_obj:
                    history_identity = {
                        "object_id": history_obj.get("id"),
                        "sha256": history_obj.get("sha256"),
                    }
                    if is_v66:
                        history_bytes = await services.storage.get(history_obj["storage_id"]).get_bytes(
                            ObjectLocation(storage_id=history_obj["storage_id"], bucket=history_obj["bucket"],
                                           key=history_obj["object_key"])
                        )
                        if hashlib.sha256(history_bytes).hexdigest() != history_obj.get("sha256"):
                            raise ExecutionRequestError(status_code=409, detail={"code": "HISTORY_INTEGRITY_MISMATCH"})
                        try:
                            planning_history = freeze_history(json.loads(history_bytes))
                            if len(planning_history) != int(session.get("history_version") or 0):
                                raise ValueError("History version does not match frozen turns")
                        except (ValueError, TypeError, AttributeError) as exc:
                            raise ExecutionRequestError(status_code=409, detail={"code": "HISTORY_INTEGRITY_MISMATCH"}) from exc
            plan_seed = dict(base_snapshot)
            plan_seed["schema_version"] = "relay-request/2.4"
            supply_cache = frozen_route.get("cache_contract") or {}
            grok_mode = None
            if provider in {"grok", "xai"} or model.lower().startswith("grok-"):
                grok_mode = supply_cache.get("grok_cache_mode", settings.relay_grok_cache_mode)
                if grok_mode not in {"unified_v2", "legacy_v1"}:
                    raise ExecutionRequestError(status_code=409, detail={"code": "CACHE_GROK_MODE_INVALID"})
            cache_upgrade = {
                "version": 1, "grok_mode": grok_mode,
                "material_budget": settings.relay_inline_cache_budget_bytes,
                "roll_alpha": settings.relay_cache_roll_alpha,
            }
            if body.requested_cache_mode != "off" and grok_mode != "legacy_v1":
                base_snapshot["input"] = canonical_structure(base_snapshot["input"])
                base_snapshot["structured_output"] = normalize_structured_output(base_snapshot["structured_output"])
                plan_seed.update(base_snapshot)
            if is_v66:
                cache_upgrade.update({
                    "version": 4 if grok_mode != "legacy_v1" else 3,
                    "fingerprint_version": 2,
                    "history_freeze_version": 1,
                    "max_uncached_tokens": settings.relay_cache_roll_max_uncached_tokens,
                    "minimum_roll_delta": settings.relay_cache_roll_min_delta_tokens,
                    "implicit_min_tokens": settings.relay_implicit_prefix_min_tokens,
                    "tail_min_chars": settings.relay_tail_turn_min_reserve_chars,
                })
                base_snapshot["history_mode"] = str(session.get("history_mode") or "cumulative")
                base_snapshot["sticky_scope"] = str(session.get("sticky_scope") or "session")
                base_snapshot["supply_group_id"] = session.get("supply_group_id")
                plan_seed.update(base_snapshot)
            plan_seed["cache_upgrade"] = cache_upgrade
            if is_v66:
                plan_seed["protocol_profile"] = frozen_route.get("protocol_profile") or {}
            context_plan = build_context_plan(
                snapshot=plan_seed,
                session=session,
                material_rows=material_rows,
                history_identity=history_identity,
                history=planning_history if planning_history is not None else ([] if is_v66 else None),
            )
            evidence_key = reuse_evidence_key(plan_seed, context_plan)
            observed_reuse: dict[str, Any] = {"observed_compatible_uses": 0}
            frozen_cache_policy = frozen_route.get("cache_policy") if isinstance(frozen_route.get("cache_policy"), dict) else {}
            policy_meta = frozen_cache_policy.get("metadata") if isinstance(frozen_cache_policy.get("metadata"), dict) else {}
            economics_policy = policy_meta.get("auto_economics") if isinstance(policy_meta.get("auto_economics"), dict) else {}
            attachment_evidence = stable_attachment_evidence(
                context_plan=context_plan,
                material_rows=material_rows,
                large_min_bytes=economics_policy.get("large_attachment_min_bytes", 1024 * 1024),
                context_policy=str(session.get("context_policy") or "conversation"),
            )
            # The frozen plan authorizes candidates. Inventory and measured
            # reuse/cost evidence are read only by the runtime arbiter.
            try:
                cache_intent = CacheIntentResolver().resolve(
                    requested_mode=body.requested_cache_mode,
                    requested_mechanism=body.requested_cache_mechanism,
                    mechanism_explicit=cache_mechanism_explicit,
                    reuse_evidence=observed_reuse,
                    attachment_evidence=attachment_evidence,
                    context_plan=context_plan,
                    cache_contract=(dict(frozen_route.get("cache_contract")) if isinstance(frozen_route.get("cache_contract"), dict) else None),
                    protocol_profile=(dict(frozen_route.get("protocol_profile")) if isinstance(frozen_route.get("protocol_profile"), dict) else None),
                    cache_policy=(dict(frozen_route.get("cache_policy")) if isinstance(frozen_route.get("cache_policy"), dict) else None),
                    cache_contract_hash=(str(frozen_route.get("cache_contract_hash")) if frozen_route.get("cache_contract_hash") else None),
                    profile_hash=(str(frozen_route.get("protocol_profile_hash")) if frozen_route.get("protocol_profile_hash") else None),
                    policy_hash=(str(frozen_route.get("cache_policy_hash")) if frozen_route.get("cache_policy_hash") else None),
                    provider=provider,
                    model=model,
                )
            except CacheDecisionError as exc:
                raise ExecutionRequestError(status_code=409, detail=exc.public_detail()) from exc
            cache_plan = freeze_dual_track_plan(
                cache_intent, snapshot={**plan_seed, "context_plan": context_plan},
                protocol_profile=frozen_route.get("protocol_profile") or {},
                contract=supply_cache, cache_policy=frozen_cache_policy,
            )
            snapshot = {
                "schema_version": "relay-request/2.4",
                **base_snapshot,
                "protocol_profile_id": frozen_route.get("protocol_profile_id"),
                "protocol_profile_hash": frozen_route.get("protocol_profile_hash"),
                "protocol_profile": (dict(frozen_route.get("protocol_profile")) if isinstance(frozen_route.get("protocol_profile"), dict) else None),
                "cache_policy_id": frozen_route.get("cache_policy_id"),
                "cache_policy_hash": frozen_route.get("cache_policy_hash"),
                "cache_policy": (dict(frozen_route.get("cache_policy")) if isinstance(frozen_route.get("cache_policy"), dict) else None),
                "cache_contract_hash": frozen_route.get("cache_contract_hash"),
                "cache_contract": (dict(frozen_route.get("cache_contract")) if isinstance(frozen_route.get("cache_contract"), dict) else None),
                "context_policy": session.get("context_policy"),
                "history_version": int(session.get("history_version") or 0),
                "history_object_hash": (history_identity or {}).get("sha256"),
                "requested_cache_mode": body.requested_cache_mode,
                "requested_cache_mechanism": body.requested_cache_mechanism,
                "requested_cache_mechanism_explicit": cache_mechanism_explicit,
                "reuse_evidence_key": evidence_key,
                "caller_intent_hash": public_intent_hash,
                "context_plan": context_plan,
                "context_plan_hash": context_plan["context_plan_hash"],
                "cache_plan": cache_plan,
                "cache_plan_hash": cache_plan["plan_hash"],
                "cache_upgrade": cache_upgrade,
            }
        else:
            snapshot = {"schema_version": "relay-request/2.2", **base_snapshot}

        req_hash = request_identity(snapshot)
        legacy_snapshot = dict(snapshot)
        legacy_req_hash: str | None = None
        if not cache_contract_session:
            legacy_snapshot["schema_version"] = "relay-request/2.1"
            legacy_snapshot.pop("options", None)
            legacy_snapshot.pop("effective_options", None)
            legacy_snapshot.pop("capability_revision", None)
            legacy_req_hash = request_identity(legacy_snapshot)

        if existing:
            allowed_hashes = {req_hash}
            if legacy_req_hash:
                allowed_hashes.add(legacy_req_hash)
            if existing.get("request_hash") not in allowed_hashes:
                raise ExecutionRequestError(status_code=409, detail="Idempotency-Key was already used for a different Request")
            if (existing.get("provider_dispatch_state") in {"not_sent", "result_stored"}
                    and existing.get("execution_mode") == "sync"):
                return await services.inline_scheduler.execute(existing)
            return existing

        request_id = uuid4()
        context_plan_object_id: str | None = None
        cache_plan_object_id: str | None = None
        if cache_contract_session and context_plan is not None and cache_plan is not None:
            context_plan_object_id = await _store_request_json_artifact(
                services,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                session_id=str(session_id),
                request_id=str(request_id),
                filename="context-plan.json",
                value=context_plan,
            )
            cache_plan_object_id = await _store_request_json_artifact(
                services,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                session_id=str(session_id),
                request_id=str(request_id),
                filename="cache-plan.json",
                value=cache_plan,
            )
        object_id = await _store_request_json_artifact(
            services,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            session_id=str(session_id),
            request_id=str(request_id),
            filename="request.json",
            value=snapshot,
        )

        job_id = str(uuid4()) if body.execution.mode == "async" else None
        accept_metadata = dict(body.metadata)
        if cache_contract_session and cache_plan is not None:
            accept_metadata["_relay_cache"] = {
                "requested_cache_mode": body.requested_cache_mode,
                "requested_cache_mechanism": body.requested_cache_mechanism,
                "reuse_evidence_key": evidence_key,
                "estimated_future_reuses": (cache_plan.get("reuse_prediction") or {}).get("expected_future_reuses"),
                "reuse_evidence_source": (cache_plan.get("reuse_prediction") or {}).get("source"),
                    "observed_compatible_uses": observed_reuse.get("observed_compatible_uses", 0),
                    "observed_implicit_uses": observed_reuse.get("observed_implicit_uses", 0),
                    "observed_implicit_hit_rate": observed_reuse.get("observed_implicit_hit_rate"),
                    "observed_implicit_samples": observed_reuse.get("observed_implicit_samples", 0),
                "selection_evidence": cache_plan.get("selection_evidence") or {},
                "planned_mechanism": cache_plan.get("planned_mechanism"),
                    "fallback_mechanisms": [
                        item.get("mechanism") for item in cache_plan.get("fallback_mechanisms", [])
                        if isinstance(item, dict)
                    ],
                "effective_cache_mechanism": cache_plan.get("planned_mechanism") if cache_plan.get("resolution_status") == "finalized" else None,
                "resolution_status": cache_plan.get("resolution_status"),
                "decision_reason": cache_plan.get("decision_reason"),
            }
        try:
            if cache_contract_session:
                if not context_plan_object_id or not cache_plan_object_id or not context_plan or not cache_plan:
                    raise RuntimeError("cache-aware Request was accepted without frozen cache artifacts")
                row = await repo.accept_request_v3(
                    session_id=str(session_id),
                    request_id=str(request_id),
                    tenant_id=tenant_id,
                    conversation_hash=conversation_hash,
                    idempotency_key=idempotency_key,
                    request_hash=req_hash,
                    caller_intent_hash=public_intent_hash,
                    request_identity_version="relay-request/2.4",
                    execution_mode=body.execution.mode,
                    request_object_id=object_id,
                    context_plan_object_id=context_plan_object_id,
                    context_plan_hash=str(context_plan["context_plan_hash"]),
                    cache_plan_object_id=cache_plan_object_id,
                    cache_plan_hash=str(cache_plan["plan_hash"]),
                    cache_resolution_status=str(cache_plan["resolution_status"]),
                    provider=provider,
                    connection_id=connection_id,
                    model=model,
                    execution_pool=session.get("execution_pool") or settings.execution_pool,
                    expected_history_version=int(session.get("history_version") or 0),
                    metadata={**accept_metadata, "_relay_audit": {"entry_point": services.entry_point}},
                    job_id=job_id,
                    route_pin=pending_pin,
                )
            else:
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
                    connection_id=connection_id,
                    model=model,
                    execution_pool=session.get("execution_pool") or settings.execution_pool,
                    metadata={**body.metadata, "_relay_audit": {"entry_point": services.entry_point}},
                    job_id=job_id,
                )
        except SupabaseError as exc:
            text = exc.body
            if "ROUTE_PIN_CONFLICT" in text or "ROUTE_GROUP_MISMATCH" in text:
                raise ExecutionRequestError(status_code=409, detail={
                    "code": "ROUTE_PIN_CONFLICT", "message": "Reload the Session and retry the same logical Request",
                }) from exc
            if "SESSION_BUSY" in text:
                raise ExecutionRequestError(status_code=409, detail="SESSION_BUSY") from exc
            if "IDEMPOTENCY_CONFLICT" in text:
                raise ExecutionRequestError(status_code=409, detail="IDEMPOTENCY_CONFLICT") from exc
            if "HISTORY_VERSION_CHANGED" in text:
                raise ExecutionRequestError(
                    status_code=409,
                    detail={
                        "code": "HISTORY_VERSION_CHANGED",
                        "message": "Session history changed while the cache/context plan was being prepared; retry the same logical request",
                    },
                ) from exc
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
            raise ExecutionRequestError(status_code=502, detail="Failed to accept Relay Request") from exc

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
            requested_cache_mode=requested_cache_mode,
            requested_cache_mechanism=(body.requested_cache_mechanism if cache_contract_session else None),
            cache_mechanism=(cache_plan or {}).get("planned_mechanism"),
            cache_decision_reason=(cache_plan or {}).get("decision_reason"),
            business_stage=(body.metadata or {}).get("stage") or (body.metadata or {}).get("purpose") or (body.input.get("stage") if isinstance(body.input, dict) else None),
        )

        if body.execution.mode == "sync":
            row = await services.inline_scheduler.execute(row)
            if not row:
                raise ExecutionRequestError(status_code=500, detail="Request disappeared after inline execution")
        return row

