from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..cache.contracts import CacheDecisionError, ExecutionFence
from ..cache.orchestrator import CacheOrchestrator
from ..cache.usage import normalize_cache_usage
from .idempotency import stable_hash
from .provider_error_observation import provider_http_error_observation
from ..materials.resolver import MaterialResolver
from ..materials.binding_resolver import BindingResolver
from ..persistence.object_storage import ObjectLocation, StorageRegistry
from ..providers.registry import ProviderRegistry
from ..providers.gemini_physical import build_gemini_physical_cache_plan, uses_physical_layout_v2
from ..providers.base import ProviderHTTPError, ProviderRequestError
from ..providers.v2_base import V2ExecutionContext
from ..observability import elapsed_ms, error as log_error, info as log_info, now_ms, exception_failure_class
from ..storage_paths import request_object_path_v2, session_history_request_path
from ..structured_output import StructuredOutputError, resolve_structured_output, validate_against_schema
from ..utils import json_bytes, truncate_utf8, utcnow
from ..v2_repository import RelayV2Repository


logger = logging.getLogger("model-relay-runtime")


class SessionConflictError(RuntimeError):
    pass


class SharedExecutionRuntime:
    """Business-agnostic v2 execution runtime used by sync and async paths."""

    def __init__(
        self,
        repo: RelayV2Repository,
        storage: StorageRegistry,
        providers: ProviderRegistry,
        materials: MaterialResolver,
        bindings: BindingResolver,
        settings: Settings,
        cache_orchestrator: CacheOrchestrator | None = None,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.providers = providers
        self.materials = materials
        self.bindings = bindings
        self.settings = settings
        self.cache = cache_orchestrator or CacheOrchestrator(repo)

    async def execute(
        self,
        request_row: dict[str, Any],
        *,
        lease_owner: str | None = None,
        lease_epoch: int | None = None,
    ) -> dict[str, Any]:
        execution_started_ms = now_ms()
        request_id = str(request_row["id"])
        session_id = str(request_row["session_id"])
        log_info(
            logger,
            "request_execution_started",
            request_id=request_id,
            session_id=session_id,
            execution_mode=request_row.get("execution_mode"),
            job_id=request_row.get("job_id"),
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
        )
        session = await self.repo.get_session(
            request_row["session_id"],
            tenant_id=request_row["tenant_id"],
            conversation_hash=request_row["conversation_hash"],
        )
        if not session:
            raise LookupError("Relay session not found")
        snapshot = await self._load_json_object(
            request_row["request_object_id"],
            tenant_id=request_row["tenant_id"],
            conversation_hash=request_row["conversation_hash"],
        )
        self._assert_frozen_route(session, snapshot, request_id=request_id)
        history: list[dict[str, Any]] = []
        if session.get("context_policy") == "conversation" and session.get("history_object_id"):
            loaded = await self._load_json_object(
                session["history_object_id"],
                tenant_id=request_row["tenant_id"],
                conversation_hash=request_row["conversation_hash"],
            )
            if isinstance(loaded, list):
                history = loaded

        material_ids = list(snapshot.get("material_ids") or [])
        if session.get("context_policy") == "conversation":
            base = session.get("material_manifest") or []
            if isinstance(base, list):
                material_ids = [str(x) for x in base] + material_ids
        # Stable order, no duplicate provider binding work.
        material_ids = list(dict.fromkeys(material_ids))

        is_v3 = str(snapshot.get("schema_version") or "") == "relay-request/2.3"
        fence: ExecutionFence | None = None
        if is_v3:
            if not lease_owner or lease_epoch is None:
                raise ProviderRequestError(
                    "CACHE_BINDING_FENCE_REJECTED",
                    "Cache-aware Request has no active execution fence",
                )
            fence = ExecutionFence(
                owner=str(lease_owner),
                epoch=int(lease_epoch),
                kind="async_job" if request_row.get("job_id") else "sync_request",
            )

        existing_binding_snapshot = request_row.get("material_binding_snapshot")
        if not isinstance(existing_binding_snapshot, list):
            existing_binding_snapshot = None
        binding_started_ms = now_ms()
        material_bindings = await self.bindings.freeze_for_request(
            material_ids=material_ids,
            connection_id=str(snapshot["connection_id"]),
            tenant_id=request_row["tenant_id"],
            conversation_hash=request_row["conversation_hash"],
            existing_snapshot=existing_binding_snapshot,
            request_id=request_id,
            session_id=session_id,
        )
        log_info(
            logger,
            "material_bindings_frozen",
            request_id=request_id,
            session_id=session_id,
            connection_id=snapshot.get("connection_id"),
            route_revision=self._route_revision(session),
            material_count=len(material_ids),
            reused_snapshot=existing_binding_snapshot is not None,
            duration_ms=elapsed_ms(binding_started_ms),
        )
        if existing_binding_snapshot is None:
            if is_v3 and fence is not None:
                installed = await self.repo.install_request_material_binding_v3(
                    request_id=request_id,
                    snapshot=material_bindings,
                    fence_owner=fence.owner,
                    fence_epoch=fence.epoch,
                )
                if not installed:
                    raise ProviderRequestError(
                        "CACHE_BINDING_FENCE_REJECTED",
                        "Material binding snapshot was rejected by the Request execution fence",
                    )
            else:
                await self.repo.update_request(
                    request_row["id"],
                    {"material_binding_snapshot": material_bindings},
                )

        context_plan = snapshot.get("context_plan") if isinstance(snapshot.get("context_plan"), dict) else None
        provider_physical_plan: dict[str, Any] | None = None
        if (
            is_v3
            and str(snapshot.get("provider") or "") == "gemini"
            and uses_physical_layout_v2(session)
        ):
            if context_plan is None:
                raise ProviderRequestError(
                    "CACHE_CONTEXT_MISMATCH",
                    "Gemini physical projection requires the frozen ContextPlan",
                )
            provider_physical_plan = build_gemini_physical_cache_plan(
                snapshot=snapshot,
                session=session,
                history=history,
                material_ids=material_ids,
                material_bindings=material_bindings,
                context_plan=context_plan,
            )
            log_info(
                logger,
                "gemini_physical_cache_plan_frozen",
                request_id=request_id,
                session_id=session_id,
                connection_id=snapshot.get("connection_id"),
                model=snapshot.get("model"),
                layout_version=provider_physical_plan.get("layout_version"),
                projector_version=provider_physical_plan.get("projector_version"),
                physical_plan_hash=provider_physical_plan.get("physical_plan_hash"),
                cached_prefix_wire_hash=provider_physical_plan.get("cached_prefix_wire_hash"),
                uncached_suffix_wire_hash=provider_physical_plan.get("uncached_suffix_wire_hash"),
                occurrence_mapping_hash=provider_physical_plan.get("occurrence_mapping_hash"),
                cacheable=provider_physical_plan.get("cacheable"),
            )

        cache_binding = None
        execution_snapshot = snapshot
        if is_v3:
            if context_plan is None or fence is None:
                raise ProviderRequestError(
                    "CACHE_CONTEXT_MISMATCH",
                    "Cache-aware Request is missing its frozen ContextPlan",
                )
            try:
                cache_binding_obj = await self.cache.prepare(
                    request_row=request_row,
                    snapshot=snapshot,
                    session=session,
                    context_plan=context_plan,
                    material_bindings=material_bindings,
                    fence=fence,
                    history=history,
                    provider_physical_plan=provider_physical_plan,
                )
            except CacheDecisionError as exc:
                raise ProviderRequestError(exc.code, exc.message) from exc
            cache_binding = cache_binding_obj.canonical(include_provider_handle=True)
            if cache_binding_obj.mechanism == "stateful_resource" and not str(cache_binding_obj.provider_handle or "").strip():
                log_error(
                    logger,
                    "cache_handle_unavailable_before_install",
                    request_id=request_id,
                    session_id=session_id,
                    connection_id=snapshot.get("connection_id"),
                    physical_plan_hash=(provider_physical_plan or {}).get("physical_plan_hash"),
                    recreate_attempted=False,
                )
                raise ProviderRequestError(
                    "CACHE_HANDLE_UNAVAILABLE",
                    "Stateful cache preparation completed without a usable Provider cache handle",
                )
            binding_hash = cache_binding_obj.binding_hash
            installed = await self.repo.install_cache_binding_v3(
                request_id=request_id,
                binding_version=cache_binding_obj.binding_version,
                plan_hash=cache_binding_obj.plan_hash,
                binding_hash=binding_hash,
                final_mechanism=cache_binding_obj.mechanism,
                resource_id=cache_binding_obj.resource_id,
                resource_generation=cache_binding_obj.resource_generation,
                metadata=cache_binding,
                fence_owner=fence.owner,
                fence_epoch=fence.epoch,
            )
            if not installed:
                raise ProviderRequestError(
                    "CACHE_BINDING_FENCE_REJECTED",
                    "Cache execution binding was rejected by the Request execution fence",
                )

            # This digest covers the exact frozen inputs supplied to the Adapter.
            # Provider-specific byte-level payload builders can strengthen this
            # to a native-wire digest without changing the dispatch transaction.
            prepared_payload_hash = stable_hash(
                {
                    "schema_version": "relay-prepared-execution/1",
                    "snapshot": snapshot,
                    "history": history,
                    "material_bindings": material_bindings,
                    "provider_physical_plan_hash": (provider_physical_plan or {}).get("physical_plan_hash"),
                    "cache_binding": cache_binding,
                }
            )
            sealed = await self.repo.seal_cache_and_dispatch_v3(
                request_id=request_id,
                binding_version=cache_binding_obj.binding_version,
                binding_hash=binding_hash,
                payload_hash=prepared_payload_hash,
                fence_owner=fence.owner,
                fence_epoch=fence.epoch,
            )
            if not sealed:
                raise ProviderRequestError(
                    "CACHE_BINDING_FENCE_REJECTED",
                    "Request dispatch seal was rejected; Provider dispatch is forbidden",
                )
            execution_snapshot = dict(snapshot)
            execution_snapshot["_relay_cache_execution"] = cache_binding
        else:
            await self.repo.update_request(
                request_row["id"],
                {
                    "provider_dispatch_state": "dispatch_started",
                    "started_at": request_row.get("started_at") or utcnow().isoformat(),
                },
            )

        adapter = self.providers.get_v2(str(snapshot["connection_id"]))
        provider_started_ms = now_ms()
        metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
        input_value = snapshot.get("input") if isinstance(snapshot.get("input"), dict) else {}
        business_stage = metadata.get("stage") or metadata.get("purpose") or input_value.get("stage")
        log_info(
            logger,
            "provider_call_started",
            request_id=request_id,
            session_id=session_id,
            provider=snapshot.get("provider"),
            connection_id=snapshot.get("connection_id"),
            model=snapshot.get("model"),
            adapter_version=getattr(adapter, "adapter_version", None),
            route_revision=self._route_revision(session),
            phase="model_inference",
            business_stage=business_stage,
            material_count=len(material_ids),
            cache_mechanism=(cache_binding or {}).get("mechanism"),
        )
        try:
            result = await adapter.execute(
                V2ExecutionContext(
                    snapshot=execution_snapshot,
                    session=session,
                    history=history,
                    material_ids=material_ids,
                    material_bindings=material_bindings,
                    tenant_id=request_row["tenant_id"],
                    conversation_hash=request_row["conversation_hash"],
                    request_id=request_id,
                    session_id=session_id,
                    context_plan=context_plan,
                    cache_execution_binding=cache_binding,
                    execution_fence=(fence.canonical() if fence is not None else None),
                    provider_physical_plan=provider_physical_plan,
                )
            )
        except Exception as exc:
            provider_error = (
                provider_http_error_observation(exc)
                if isinstance(exc, ProviderHTTPError)
                else None
            )
            log_error(
                logger,
                "provider_call_failed",
                exc_info=(False if provider_error is not None else True),
                request_id=request_id,
                session_id=session_id,
                provider=snapshot.get("provider"),
                connection_id=snapshot.get("connection_id"),
                model=snapshot.get("model"),
                adapter_version=getattr(adapter, "adapter_version", None),
                route_revision=self._route_revision(session),
                phase=(provider_error or {}).get("phase") or getattr(exc, "phase", None) or "model_inference",
                duration_ms=elapsed_ms(provider_started_ms),
                failure_class=exception_failure_class(exc),
                upstream_http_status=(provider_error or {}).get("status") or getattr(exc, "status_code", None),
                upstream_request_id=(provider_error or {}).get("request_id"),
                provider_http_error=provider_error,
                exception_type=type(exc).__name__,
                stream_interrupted=getattr(exc, "stream_interrupted", False),
                bytes_received=getattr(exc, "bytes_received", None),
            )
            raise
        log_info(
            logger,
            "provider_call_completed",
            request_id=request_id,
            session_id=session_id,
            provider=snapshot.get("provider"),
            connection_id=snapshot.get("connection_id"),
            model=snapshot.get("model"),
            adapter_version=getattr(adapter, "adapter_version", None),
            route_revision=self._route_revision(session),
            phase="model_inference",
            duration_ms=elapsed_ms(provider_started_ms),
            http_status=result.http_status,
            upstream_request_id=result.provider_request_id,
            provider_response_id=result.response_id,
            response_bytes=len(result.raw_bytes),
        )

        raw_object_id = await self._store_object(
            request_row,
            "raw-response.json",
            result.raw_bytes,
            "application/json",
        )
        output_object_id = await self._store_object(
            request_row,
            "response-output.json",
            json_bytes(result.response_output),
            "application/json",
        )
        # Persist the upstream success body before any Relay parser/schema gate.
        # If validation fails, the original 2xx entity remains available for
        # diagnosis instead of being replaced by a synthetic Relay error.
        if not is_v3:
            await self.repo.update_request(
                request_row["id"],
                {
                    "provisional_result_object_id": raw_object_id,
                    "provisional_output_object_id": output_object_id,
                    "provider_response_id": result.response_id,
                },
            )

        self._check_observed_contract(
            snapshot,
            result.observed or {},
            request_id=request_id,
            session_id=session_id,
            raw_object_id=raw_object_id,
            output_object_id=output_object_id,
        )

        spec = resolve_structured_output(snapshot, fallback_name="structured_output")
        if spec is not None and spec.mode == "json_schema":
            try:
                parsed = json.loads(result.text)
                validate_against_schema(parsed, spec)
            except Exception as exc:
                if isinstance(exc, StructuredOutputError):
                    structured_exc = exc
                else:
                    structured_exc = StructuredOutputError(
                        "STRUCTURED_OUTPUT_INVALID_JSON",
                        f"Provider output is not valid JSON: {exc}",
                    )
                    structured_exc.__cause__ = exc
                setattr(structured_exc, "provider_success_object_id", raw_object_id)
                setattr(structured_exc, "provider_output_object_id", output_object_id)
                raise structured_exc

        full_text_object_id = None
        visible_text = result.text
        text_truncated = False
        if len(visible_text.encode("utf-8")) > self.settings.relay_result_soft_limit_bytes:
            full_text_object_id = await self._store_object(
                request_row,
                "visible-result.json",
                json_bytes({"text": visible_text}),
                "application/json",
            )
            visible_text = truncate_utf8(visible_text, self.settings.relay_result_preview_bytes)
            text_truncated = True

        compact_result = {
            "request_id": request_row["id"],
            "status": "succeeded",
            "text": visible_text,
            "response_id": result.response_id,
            "usage": result.usage,
            "cached_tokens": result.cached_tokens,
            "text_truncated": text_truncated,
            "full_text_object_id": full_text_object_id,
            "raw_response_object_id": raw_object_id,
            "response_output_object_id": output_object_id,
            "observed": result.observed or {},
        }
        cache_usage = None
        if is_v3:
            plan = snapshot.get("cache_plan") if isinstance(snapshot.get("cache_plan"), dict) else {}
            usage_observation = normalize_cache_usage(
                protocol=(str(snapshot.get("protocol")) if snapshot.get("protocol") else None),
                usage=result.usage,
                effective_mechanism=(cache_binding or {}).get("mechanism"),
                requested_mode=str(snapshot.get("requested_cache_mode") or "auto"),
            )
            cache_usage = usage_observation.canonical()
            compact_result["cache"] = {
                "requested_cache_mode": snapshot.get("requested_cache_mode"),
                "planned_mechanism": plan.get("planned_mechanism"),
                "effective_cache_mechanism": (cache_binding or {}).get("mechanism"),
                "execution_mechanism": (cache_binding or {}).get("mechanism"),
                # At this point the physical CacheExecutionBinding has been
                # installed and sealed, so any final-threshold guard is resolved.
                "resolution_status": "finalized",
                "decision_reason": (cache_binding or {}).get("decision_reason") or plan.get("decision_reason"),
                **cache_usage,
            }
        if len(json_bytes(compact_result)) > self.settings.relay_result_hard_limit_bytes:
            compact_result["text"] = truncate_utf8(
                str(compact_result.get("text") or ""),
                min(self.settings.relay_result_preview_bytes, 196608),
            )
            compact_result["text_truncated"] = True

        history_object_id = None
        if session.get("context_policy") == "conversation":
            new_history = list(history)
            entry = {
                "request_id": request_row["id"],
                "created_at": utcnow().isoformat(),
            }
            entry.update(result.history_entry)
            new_history.append(entry)
            history_object_id = await self._store_history(
                request_row,
                new_history,
                int(request_row["expected_history_version"]) + 1,
            )

        if is_v3:
            assert fence is not None
            stored = await self.repo.store_result_v3(
                request_id=request_id,
                result_object_id=raw_object_id,
                output_object_id=output_object_id,
                history_object_id=history_object_id,
                compact_result=compact_result,
                provider_response_id=result.response_id,
                cache_usage=cache_usage,
                fence_owner=fence.owner,
                fence_epoch=fence.epoch,
            )
            if not stored:
                raise SessionConflictError(
                    "Provider result was archived but a stale execution fence could not publish result_stored"
                )
            ok = await self.repo.complete_request_v3(
                request_id=request_id,
                session_id=session_id,
                history_object_id=history_object_id,
                result_object_id=raw_object_id,
                output_object_id=output_object_id,
                compact_result=compact_result,
                provider_response_id=result.response_id,
                expected_history_version=int(request_row["expected_history_version"]),
                fence_owner=fence.owner,
                fence_epoch=fence.epoch,
            )
        else:
            await self.repo.update_request(
                request_row["id"],
                {
                    "provider_dispatch_state": "result_stored",
                    "provisional_result_object_id": raw_object_id,
                    "provisional_output_object_id": output_object_id,
                    "provisional_history_object_id": history_object_id,
                    "provisional_compact_result": compact_result,
                    "provider_response_id": result.response_id,
                },
            )
            ok = await self.repo.complete_request(
                request_id=request_row["id"],
                session_id=request_row["session_id"],
                history_object_id=history_object_id,
                result_object_id=raw_object_id,
                output_object_id=output_object_id,
                compact_result=compact_result,
                provider_response_id=result.response_id,
                expected_history_version=int(request_row["expected_history_version"]),
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
            )
        if not ok:
            raise SessionConflictError(
                "Request result was persisted but the atomic Session commit was rejected"
            )
        log_info(
            logger,
            "request_execution_committed",
            request_id=request_id,
            session_id=session_id,
            provider=snapshot.get("provider"),
            connection_id=snapshot.get("connection_id"),
            model=snapshot.get("model"),
            history_version=int(request_row["expected_history_version"]) + (1 if history_object_id else 0),
            duration_ms=elapsed_ms(execution_started_ms),
            status="succeeded",
        )
        return compact_result

    @staticmethod
    def _route_revision(session: dict[str, Any]) -> str | None:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        route = metadata.get("_relay_route") if isinstance(metadata.get("_relay_route"), dict) else {}
        return str(route.get("route_revision")) if route.get("route_revision") else None

    @staticmethod
    def _check_observed_contract(
        snapshot: dict[str, Any],
        observed: dict[str, Any],
        *,
        request_id: str,
        session_id: str,
        raw_object_id: str,
        output_object_id: str,
    ) -> None:
        """Validate facts observed after a successful upstream response.

        The check intentionally runs only after the raw success entity has been
        archived. A strict mismatch therefore becomes a Relay contract failure
        without discarding the upstream evidence, and it never triggers a second
        provider dispatch.
        """

        policy = str(snapshot.get("observed_model_policy") or "audit").strip().lower()
        if policy == "ignore":
            return

        expected_model = str(snapshot.get("model") or "").strip()
        actual_model = str(observed.get("actual_model") or "").strip()
        mismatch = bool(expected_model and actual_model and expected_model.lower() != actual_model.lower())
        missing = bool(expected_model and not actual_model)

        fields = {
            "request_id": request_id,
            "session_id": session_id,
            "provider": snapshot.get("provider"),
            "expected_model": expected_model or None,
            "actual_model": actual_model or None,
            "offering_id": snapshot.get("offering_id"),
            "channel_id": snapshot.get("channel_id"),
            "protocol": snapshot.get("protocol"),
            "aihubmix_fallback": observed.get("aihubmix_fallback"),
            "aihubmix_router_resolved_model": observed.get("aihubmix_router_resolved_model"),
            "json_repaired": observed.get("json_repaired"),
            "observed_model_policy": policy,
        }

        if mismatch or missing:
            event = "upstream_model_contract_mismatch" if mismatch else "upstream_model_unobserved"
            if policy == "strict":
                log_error(logger, event, failure_class="provider_contract", **fields)
                code = "UPSTREAM_MODEL_CONTRACT_MISMATCH" if mismatch else "UPSTREAM_MODEL_UNOBSERVED"
                message = (
                    f"Upstream executed model {actual_model!r}, but the frozen Request requires {expected_model!r}"
                    if mismatch
                    else f"Upstream response did not expose an observable model identity for frozen model {expected_model!r}"
                )
                exc = ProviderRequestError(code, message)
                setattr(exc, "provider_success_object_id", raw_object_id)
                setattr(exc, "provider_output_object_id", output_object_id)
                raise exc
            log_info(logger, event, failure_class="provider_contract_audit", **fields)
            return

        log_info(logger, "upstream_model_contract_observed", **fields)

    def _assert_frozen_route(
        self,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        request_id: str,
    ) -> None:
        expected = {
            "provider": str(session.get("provider") or ""),
            "model": str(session.get("model") or ""),
            "connection_id": str(session.get("connection_id") or ""),
        }
        actual = {
            "provider": str(snapshot.get("provider") or ""),
            "model": str(snapshot.get("model") or ""),
            "connection_id": str(snapshot.get("connection_id") or ""),
        }
        if actual != expected:
            log_error(
                logger,
                "request_route_snapshot_mismatch",
                request_id=request_id,
                session_id=session.get("id"),
                expected_provider=expected["provider"],
                actual_provider=actual["provider"],
                expected_model=expected["model"],
                actual_model=actual["model"],
                expected_connection_id=expected["connection_id"],
                actual_connection_id=actual["connection_id"],
                route_revision=self._route_revision(session),
                failure_class="relay_validation",
            )
            raise ProviderRequestError(
                "ROUTE_BINDING_MISMATCH",
                "Request snapshot route does not match the frozen Session route",
            )

        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        route = metadata.get("_relay_route") if isinstance(metadata.get("_relay_route"), dict) else None
        if route is not None:
            frozen_connection = str(route.get("connection_id") or "")
            if frozen_connection and frozen_connection != expected["connection_id"]:
                log_error(
                    logger,
                    "session_route_metadata_mismatch",
                    request_id=request_id,
                    session_id=session.get("id"),
                    connection_id=expected["connection_id"],
                    route_metadata_connection_id=frozen_connection,
                    route_revision=route.get("route_revision"),
                    failure_class="relay_validation",
                )
                raise ProviderRequestError(
                    "ROUTE_BINDING_MISMATCH",
                    "Session route metadata does not match its frozen connection",
                )

            frozen_capability = str(route.get("capability_revision") or "")
            request_capability = str(snapshot.get("capability_revision") or "")
            if frozen_capability and request_capability and frozen_capability != request_capability:
                raise ProviderRequestError(
                    "CAPABILITY_BINDING_MISMATCH",
                    "Request model-option capability revision does not match the frozen Session capability revision",
                )
            frozen_contract_hash = str(route.get("capability_contract_hash") or "")
            request_contract_hash = str(snapshot.get("capability_contract_hash") or "")
            if frozen_contract_hash and request_contract_hash != frozen_contract_hash:
                raise ProviderRequestError(
                    "CAPABILITY_BINDING_MISMATCH",
                    "Request capability-contract snapshot does not match the frozen Session contract",
                )
            frozen_route_hash = str(route.get("route_binding_hash") or "")
            request_route_hash = str(snapshot.get("route_binding_hash") or "")
            if frozen_route_hash and request_route_hash != frozen_route_hash:
                raise ProviderRequestError(
                    "ROUTE_BINDING_MISMATCH",
                    "Request route binding hash does not match the frozen Session route",
                )
            if str(snapshot.get("schema_version") or "") == "relay-request/2.3":
                for key, code in (
                    ("protocol_profile_hash", "CACHE_CONTEXT_MISMATCH"),
                    ("cache_policy_hash", "CACHE_CONTEXT_MISMATCH"),
                    ("cache_contract_hash", "CACHE_CONTEXT_MISMATCH"),
                ):
                    frozen_value = str(route.get(key) or "")
                    request_value = str(snapshot.get(key) or "")
                    if frozen_value and request_value != frozen_value:
                        raise ProviderRequestError(
                            code,
                            f"Request {key} does not match the frozen Session cache contract",
                        )

        adapter_meta = self.providers.describe(expected["connection_id"])
        if adapter_meta is None:
            raise ProviderRequestError(
                "ROUTE_ADAPTER_NOT_REGISTERED",
                "The Session's frozen Relay route has no registered inference adapter on this worker",
            )
        registered_provider = str(adapter_meta.get("provider") or "")
        # Old pre-route sessions do not carry _relay_route and may have legacy
        # provider labels. New route-frozen Sessions are strict.
        if route is not None and registered_provider and registered_provider not in {"*", expected["provider"].lower()}:
            raise ProviderRequestError(
                "ROUTE_BINDING_MISMATCH",
                "The frozen Session provider does not match the registered Adapter",
            )
        if route is not None:
            frozen_protocol = str(route.get("protocol") or "").lower()
            adapter_protocol = str(adapter_meta.get("protocol") or "").lower()
            if frozen_protocol and adapter_protocol and frozen_protocol != adapter_protocol:
                raise ProviderRequestError(
                    "ROUTE_BINDING_MISMATCH",
                    "The frozen Session protocol does not match the registered Adapter",
                )

    async def _load_json_object(
        self, object_id: str, *, tenant_id: str, conversation_hash: str
    ) -> Any:
        obj = await self.repo.get_object(
            object_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        if not obj:
            raise LookupError(f"Object not found: {object_id}")
        data = await self.storage.get(obj["storage_id"]).get_bytes(
            ObjectLocation(obj["storage_id"], obj["bucket"], obj["object_key"])
        )
        return json.loads(data.decode("utf-8"))

    async def _store_object(
        self,
        request_row: dict[str, Any],
        filename: str,
        data: bytes,
        content_type: str,
    ) -> str:
        import hashlib

        object_id = f"obj_{uuid4().hex}"
        path = request_object_path_v2(
            self.settings,
            request_row["tenant_id"],
            request_row["conversation_hash"],
            request_row["session_id"],
            request_row["id"],
            filename,
        )
        backend = self.storage.get(self.settings.default_storage_id)
        location = await backend.put_bytes(path, data, content_type=content_type)
        await self.repo.create_object(
            {
                "id": object_id,
                "tenant_id": request_row["tenant_id"],
                "conversation_hash": request_row["conversation_hash"],
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

    async def _store_history(
        self,
        request_row: dict[str, Any],
        history: list[dict[str, Any]],
        next_version: int,
    ) -> str:
        import hashlib

        data = json_bytes(history)
        object_id = f"obj_{uuid4().hex}"
        path = session_history_request_path(
            self.settings,
            request_row["tenant_id"],
            request_row["conversation_hash"],
            request_row["session_id"],
            next_version,
            request_row["id"],
        )
        backend = self.storage.get(self.settings.default_storage_id)
        location = await backend.put_bytes(path, data, content_type="application/json")
        await self.repo.create_object(
            {
                "id": object_id,
                "tenant_id": request_row["tenant_id"],
                "conversation_hash": request_row["conversation_hash"],
                "storage_id": location.storage_id,
                "bucket": location.bucket,
                "object_key": location.key,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "content_type": "application/json",
                "created_at": utcnow().isoformat(),
            }
        )
        return object_id
