from __future__ import annotations

from datetime import datetime, timezone
import math
import logging
from typing import Any, Protocol

import httpx

from ..core.idempotency import stable_hash
from ..core.provider_error_observation import (
    provider_http_error_observation,
    pseudonymize_provider_request_id,
    safe_exception_observation,
)
from ..observability import error as log_error, info as log_info, warning as log_warning
from ..providers.base import ProviderHTTPError
from .contracts import CacheDecisionError, ExecutionFence
from .economics import (
    evaluate_new_resource,
    evaluate_current_read_resource,
    evaluate_relative_cache_strategy,
)


logger = logging.getLogger("model-relay-cache")


class CacheResourceAdapter(Protocol):
    adapter_version: str

    def build_spec(
        self,
        *,
        snapshot: dict[str, Any],
        history: list[dict[str, Any]],
        context_plan: dict[str, Any],
        material_bindings: list[dict[str, Any]],
        session: dict[str, Any],
        ttl_seconds: int | None,
        provider_physical_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def measure(self, *, spec: dict[str, Any]) -> int: ...
    async def create(self, *, spec: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]: ...
    async def get(self, *, handle: str, operation: dict[str, Any] | None = None) -> dict[str, Any]: ...
    async def renew(self, *, handle: str, expire_at: str, operation: dict[str, Any] | None = None) -> dict[str, Any]: ...
    async def delete(self, *, handle: str, operation: dict[str, Any] | None = None) -> dict[str, Any]: ...


class StatefulResourceManager:
    def __init__(self, repo: Any, registry: Any) -> None:
        self.repo = repo
        self.registry = registry

    async def prepare(
        self,
        *,
        request_row: dict[str, Any],
        plan: dict[str, Any],
        context_plan: dict[str, Any],
        material_bindings: list[dict[str, Any]],
        fence: ExecutionFence,
        snapshot: dict[str, Any],
        session: dict[str, Any],
        history: list[dict[str, Any]],
        provider_physical_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        connection_id = str(snapshot.get("connection_id") or "")
        adapter = self.registry.maybe_get(connection_id) if self.registry is not None else None
        if adapter is None:
            return self._prepare_failure(
                plan,
                reason="stateful_adapter_unavailable",
                message="Stateful cache resource adapter is not registered for the frozen connection",
            )

        cfg = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
        scope_hash = self.repo.cache_scope_hash(
            tenant_id=str(request_row["tenant_id"]),
            conversation_hash=str(request_row["conversation_hash"]),
            session_id=str(request_row["session_id"]),
            offering_id=str(snapshot.get("offering_id") or ""),
            account_scope_hash=str((session.get("metadata") or {}).get("_relay_route", {}).get("account_scope_hash") or ""),
            protocol_profile_hash=str(plan.get("profile_hash") or ""),
        )
        build_kwargs: dict[str, Any] = {
            "snapshot": snapshot,
            "history": history,
            "context_plan": context_plan,
            "material_bindings": material_bindings,
            "session": session,
            "ttl_seconds": (int(cfg["ttl_seconds"]) if isinstance(cfg.get("ttl_seconds"), int) else None),
        }
        if provider_physical_plan is not None:
            build_kwargs["provider_physical_plan"] = provider_physical_plan
        spec = adapter.build_spec(**build_kwargs)
        if not bool(spec.get("cacheable")):
            cacheability_reason = str(spec.get("cacheability_reason") or "no_cacheable_contents")
            if (plan.get("requested_mode") == "on"
                and plan.get("requested_mechanism") == "stateful_resource"):
                raise CacheDecisionError(
                    "CACHE_PREFIX_INELIGIBLE",
                    f"Forced explicit cache cannot preserve the complete context: {cacheability_reason}",
                    reason=cacheability_reason,
                )
            if cacheability_reason not in {"no_cacheable_contents"}:
                return self._prepare_failure(
                    plan,
                    reason=cacheability_reason,
                    message=f"Gemini cache prefix is not safe to prepare: {cacheability_reason}",
                )
            return {
                "mechanism": None,
                "decision_reason": "no_cacheable_prefix",
                "metadata": {
                    "provider_measurement": "not_applicable",
                    "cacheability_reason": cacheability_reason,
                    **self._spec_binding_metadata(spec),
                },
            }

        content_fingerprint = str(spec.get("content_fingerprint") or "")
        reuse_key = str(spec.get("reuse_key") or "")
        prefix_version = int(spec.get("prefix_version") or 0)
        compatible = (
            spec.get("compatible_prefix_fingerprints")
            if isinstance(spec.get("compatible_prefix_fingerprints"), dict)
            else {}
        )
        measurement_order = str(spec.get("measurement_order") or "")
        measure_before_lookup = measurement_order == "before_lookup"
        provider_create_threshold = measurement_order == "provider_create"
        token_count: int | None = None

        # Legacy layouts may pre-measure the exact frozen cached prefix. Layout/4+
        # use Provider create as the authoritative threshold gate. Layout/5
        # introduced SDK cache creation; layout/6 removed Gemini File references
        # from CachedContent; layout/7+ freezes systemInstruction into the
        # CachedContent resource; layout/8 also requires non-empty contents before
        # StatefulResourceManager may proceed to lookup/create.
        if measure_before_lookup:
            measured = await self._measure(
                adapter=adapter,
                spec=spec,
                plan=plan,
                cfg=cfg,
                request_row=request_row,
                snapshot=snapshot,
            )
            if isinstance(measured, dict):
                return measured
            token_count = measured

        # Reuse is compatible-prefix based. Legacy 3.1 Sessions keep the old
        # growing-history behavior; physical-layout v2 uses a fixed Session base.
        candidates = await self.repo.find_compatible_cache_resources(
            scope_hash=scope_hash,
            reuse_key=reuse_key,
            max_prefix_version=prefix_version,
        )
        prefix_growth_decision: dict[str, Any] | None = None
        growth_fallback: tuple[dict[str, Any], int, dict[str, Any]] | None = None
        for existing in candidates:
            existing_version = int(existing.get("prefix_version") or 0)
            expected = str(compatible.get(str(existing_version)) or "")
            if not expected or expected != str(existing.get("content_fingerprint") or ""):
                continue
            handle = str(existing.get("provider_handle_ref") or "")
            if not handle:
                log_warning(
                    logger,
                    "cache_resource_missing_handle_no_recreate",
                    request_id=request_row.get("id"),
                    session_id=request_row.get("session_id"),
                    connection_id=connection_id,
                    resource_id=existing.get("id"),
                    content_fingerprint=content_fingerprint,
                )
                continue
            try:
                observation = await adapter.get(handle=handle)
            except ProviderHTTPError as exc:
                error_observation = provider_http_error_observation(exc)
                log_error(
                    logger,
                    "cache_resource_probe_provider_error",
                    request_id=request_row.get("id"),
                    session_id=request_row.get("session_id"),
                    connection_id=connection_id,
                    resource_id=existing.get("id"),
                    provider_error=error_observation,
                )
                if exc.status_code in {404, 410}:
                    await self.repo.invalidate_cache_resource(
                        resource_id=str(existing["id"]),
                        provider_handle_ref=handle,
                        state="expired" if exc.status_code == 410 else "invalid",
                    )
                    continue
                return self._provider_prepare_failure(plan, exc, "cache_resource_probe_failed")
            except (httpx.HTTPError, RuntimeError) as exc:
                log_error(
                    logger,
                    "cache_resource_probe_exception",
                    request_id=request_row.get("id"),
                    session_id=request_row.get("session_id"),
                    connection_id=connection_id,
                    resource_id=existing.get("id"),
                    error=safe_exception_observation(exc, phase="cache_resource_probe"),
                )
                return self._provider_prepare_failure(plan, exc, "cache_resource_probe_failed")

            if self._observation_expiring(observation):
                await self.repo.invalidate_cache_resource(
                    resource_id=str(existing["id"]),
                    provider_handle_ref=handle,
                    state="expired",
                )
                continue

            growth_decision: dict[str, Any] | None = None
            if existing_version < prefix_version:
                should_grow, growth_decision = await self._evaluate_prefix_growth(
                    adapter=adapter,
                    existing=existing,
                    spec=spec,
                    cfg=cfg,
                    request_row=request_row,
                    snapshot=snapshot,
                )
                log_info(
                    logger,
                    "cache_prefix_growth_decision",
                    request_id=request_row.get("id"),
                    session_id=request_row.get("session_id"),
                    connection_id=connection_id,
                    existing_prefix_version=existing_version,
                    target_prefix_version=prefix_version,
                    decision=("create_longer_prefix" if should_grow else "reuse_existing_prefix"),
                    growth=growth_decision,
                )
                if should_grow:
                    # The old immutable resource remains valid. Continue through
                    # the existing fenced create-operation path for a new generation.
                    prefix_growth_decision = growth_decision
                    growth_fallback = (existing, existing_version, observation)
                    if growth_decision.get("target_prefix_tokens") is not None:
                        token_count = int(growth_decision["target_prefix_tokens"])
                    break
            else:
                growth_decision = {
                    "decision": "reuse_exact_prefix",
                    "reason": "resource_already_covers_current_prefix",
                }
            return self._binding_from_resource(
                existing,
                decision_reason="resource_reused",
                cached_history_version=existing_version,
                token_count=token_count if token_count is not None else existing.get("token_count"),
                reuse_key=reuse_key,
                spec=spec,
                provider_observation=observation,
                growth_decision=growth_decision,
            )

        if token_count is None and not provider_create_threshold:
            measured = await self._measure(
                adapter=adapter,
                spec=spec,
                plan=plan,
                cfg=cfg,
                request_row=request_row,
                snapshot=snapshot,
            )
            if isinstance(measured, dict):
                return measured
            token_count = measured

        # Every new immutable generation is a billable provider side effect.
        # Reuse above remains permitted even when creation economics are unknown.
        # The execution-phase guard consumes only frozen prices and facts; it
        # never chooses another mechanism or alters the Session route.
        economics_cfg = cfg.get("auto_economics") if isinstance(cfg.get("auto_economics"), dict) else {}
        # Persisted Requests retain the economics frozen by their own resolver
        # version. Only newly accepted resolver/4+ plans use an execution gate.
        resolver_version = str(plan.get("resolver_version") or "")
        new_economic_contract = resolver_version in {
            "relay-cache-resolver/4", "relay-cache-resolver/5", "relay-cache-resolver/6"
        }
        needs_economic_gate = new_economic_contract and (
            plan.get("requested_mode") == "auto" or (
                plan.get("requested_mode") == "on"
                and economics_cfg.get("max_new_resource_cost") is not None
            )
        )
        cache_creation_decision: dict[str, Any] | None = None

        def reuse_existing_when_growth_declined(reason: str) -> dict[str, Any] | None:
            if growth_fallback is None:
                return None
            previous, version, previous_observation = growth_fallback
            return self._binding_from_resource(
                previous,
                decision_reason="resource_reused_growth_declined",
                cached_history_version=version,
                token_count=previous.get("token_count"), reuse_key=reuse_key,
                spec=spec, provider_observation=previous_observation,
                growth_decision={
                    **(prefix_growth_decision or {}),
                    "decision": "reuse", "reason": reason,
                },
            )

        if needs_economic_gate:
            if token_count is None:
                try:
                    token_count = int(await adapter.measure(spec=spec))
                except Exception as exc:
                    log_warning(
                        logger, "cache_new_resource_cost_measurement_unavailable",
                        request_id=request_row.get("id"),
                        session_id=request_row.get("session_id"),
                        error_type=type(exc).__name__,
                    )
                    if plan.get("requested_mode") == "auto":
                        reuse = reuse_existing_when_growth_declined("new_resource_cost_measurement_unavailable")
                        if reuse is not None:
                            return reuse
                        return {
                            "mechanism": None,
                            "decision_reason": "new_resource_cost_measurement_unavailable",
                            "metadata": {"cache_cost_decision": {"allow": False, "reason": "measurement_unavailable"}},
                        }
                    raise CacheDecisionError(
                        "CACHE_COST_UNVERIFIED",
                        "Forced resource would exceed an unverifiable frozen cost budget",
                    ) from exc
            if prefix_growth_decision is not None and prefix_growth_decision.get("decision") == "grow":
                # A previously authorized, measured growth decision may still
                # be checked against any separately frozen hard budget.
                frozen_growth = cfg.get("prefix_growth") if isinstance(cfg.get("prefix_growth"), dict) else {}
                if not economics_cfg.get("price_schedule") and frozen_growth:
                    economics_cfg = {**economics_cfg, "price_schedule": frozen_growth}
            if (resolver_version == "relay-cache-resolver/6" and
                economics_cfg.get("new_economics_policy") == "relative_cache/2"):
                baseline_tokens = 0
                if growth_fallback is not None:
                    try:
                        baseline_tokens = int(growth_fallback[0].get("token_count") or 0)
                    except (TypeError, ValueError, OverflowError):
                        baseline_tokens = 0
                cache_creation_decision = evaluate_relative_cache_strategy(
                    tokens=token_count,
                    ttl_seconds=int(spec.get("ttl_seconds") or 3600),
                    config=economics_cfg,
                    forced=bool(plan.get("requested_mode") == "on"),
                    already_cached_tokens=max(0, min(baseline_tokens, token_count)),
                )
            elif (resolver_version == "relay-cache-resolver/5" and
                economics_cfg.get("new_economics_policy") == "current_read/1"):
                baseline_tokens = 0
                if growth_fallback is not None:
                    try:
                        baseline_tokens = int(growth_fallback[0].get("token_count") or 0)
                    except (TypeError, ValueError, OverflowError):
                        baseline_tokens = 0
                cache_creation_decision = evaluate_current_read_resource(
                    tokens=token_count,
                    ttl_seconds=int(spec.get("ttl_seconds") or 3600),
                    config=economics_cfg,
                    forced=bool(plan.get("requested_mode") == "on"),
                    already_cached_tokens=max(0, min(baseline_tokens, token_count)),
                )
            else:
                # Respect persisted 6.0.0 Request plans during crash recovery.
                cache_creation_decision = evaluate_new_resource(
                    tokens=token_count,
                    ttl_seconds=int(spec.get("ttl_seconds") or 3600),
                    config=economics_cfg,
                    forced=bool(plan.get("requested_mode") == "on"),
                )
            if not cache_creation_decision.get("allow"):
                log_info(
                    logger, "cache_new_resource_creation_skipped",
                    request_id=request_row.get("id"),
                    session_id=request_row.get("session_id"),
                    reason=cache_creation_decision.get("reason"),
                    estimated_net_savings=(
                        cache_creation_decision.get("estimated_net_savings_input_equivalents")
                        if cache_creation_decision.get("estimated_net_savings_input_equivalents") is not None
                        else cache_creation_decision.get("estimated_net_savings_after_margin")
                    ),
                )
                if plan.get("requested_mode") == "on":
                    raise CacheDecisionError(
                        "CACHE_NEW_RESOURCE_BUDGET_DENIED",
                        "Forced explicit cache exceeds the frozen creation budget or lacks required cost evidence",
                        reason=str(cache_creation_decision.get("reason") or "unknown"),
                    )
                reuse = reuse_existing_when_growth_declined(
                    str(cache_creation_decision.get("reason") or "cost_gate_rejected")
                )
                if reuse is not None:
                    return reuse
                return {
                    "mechanism": None,
                    "decision_reason": str(cache_creation_decision.get("reason") or "cost_gate_rejected"),
                    "metadata": {"cache_cost_decision": cache_creation_decision},
                }

        try:
            operation = await self.repo.create_cache_operation_intent(
                request_id=str(request_row["id"]),
                scope_hash=scope_hash,
                content_fingerprint=content_fingerprint,
                operation_type="create",
                lease_owner=fence.owner,
                lease_epoch=fence.epoch,
            )
        except Exception as exc:
            # A create_unknown operation intentionally cannot be reclaimed. The
            # only action here is a read-after-race; never issue another POST.
            log_warning(
                logger,
                "cache_create_not_claimed_no_recreate",
                request_id=request_row.get("id"),
                session_id=request_row.get("session_id"),
                connection_id=connection_id,
                content_fingerprint=content_fingerprint,
                error=safe_exception_observation(exc, phase="cache_create_claim"),
            )
            raced = await self.repo.find_compatible_cache_resources(
                scope_hash=scope_hash,
                reuse_key=reuse_key,
                max_prefix_version=prefix_version,
            )
            for row in raced:
                if str(row.get("content_fingerprint") or "") == content_fingerprint and row.get("provider_handle_ref"):
                    return self._binding_from_resource(
                        row,
                        decision_reason="resource_reused_after_race",
                        cached_history_version=int(row.get("prefix_version") or prefix_version),
                        token_count=token_count,
                        reuse_key=reuse_key,
                        spec=spec,
                    )
            return self._provider_prepare_failure(plan, exc, "cache_create_not_claimed")

        op_id = str(operation["op_id"])
        op_owner = str(operation.get("lease_owner") or fence.owner)
        op_epoch = int(operation.get("lease_epoch") or 0)
        started = await self.repo.start_cache_operation(
            operation_id=op_id,
            lease_owner=op_owner,
            lease_epoch=op_epoch,
        )
        if not started:
            return self._prepare_failure(
                plan,
                reason="cache_operation_fence_rejected",
                message="Cache create operation lost its fencing token before Provider dispatch",
            )

        # From this point onward the Provider-side create may have happened. An
        # exception or a missing handle is recorded as unknown and is NOT a
        # license to POST create again in this execution.
        try:
            observation = await adapter.create(spec=spec, operation=operation)
        except ProviderHTTPError as exc:
            error_observation = provider_http_error_observation(exc)
            state = "failed" if 400 <= int(exc.status_code) < 500 else "unknown"
            raw_result = {
                "outcome": "cache_create_rejected" if state == "failed" else "cache_create_unknown",
                "provider_http_error": error_observation,
            }
            await self._record_error_observation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                raw_result=raw_result,
                provider_request_id=error_observation.get("request_id"),
            )
            await self.repo.finish_cache_operation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                state=state,
                raw_result=raw_result,
            )
            log_error(
                logger,
                "cache_create_provider_http_error",
                request_id=request_row.get("id"),
                session_id=request_row.get("session_id"),
                connection_id=connection_id,
                cache_operation_id=op_id,
                operation_state=state,
                provider_error=error_observation,
                recreate_attempted=False,
            )
            return self._provider_prepare_failure(
                plan,
                exc,
                "cache_create_rejected" if state == "failed" else "cache_create_unknown",
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            error_observation = safe_exception_observation(exc, phase="cache_create")
            raw_result = {
                "outcome": "cache_create_unknown",
                "exception": error_observation,
                "recreate_attempted": False,
            }
            await self._record_error_observation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                raw_result=raw_result,
            )
            await self.repo.finish_cache_operation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                state="unknown",
                raw_result=raw_result,
            )
            log_error(
                logger,
                "cache_create_exception_no_recreate",
                request_id=request_row.get("id"),
                session_id=request_row.get("session_id"),
                connection_id=connection_id,
                cache_operation_id=op_id,
                error=error_observation,
                recreate_attempted=False,
            )
            return self._provider_prepare_failure(plan, exc, "cache_create_unknown")

        if not isinstance(observation, dict):
            observation = {}
        if token_count is None:
            token_count = self._token_count_from_create_observation(observation)
        handle = str(observation.get("handle") or "").strip()
        if not handle:
            provider_request_id = pseudonymize_provider_request_id(
                str(observation.get("provider_request_id") or "") or None
            )
            raw_result = {
                "outcome": "cache_handle_unavailable",
                "phase": "cache_create",
                "provider_request_id": provider_request_id,
                "recreate_attempted": False,
            }
            await self._record_error_observation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                raw_result=raw_result,
                provider_request_id=provider_request_id,
            )
            await self.repo.finish_cache_operation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                state="unknown",
                raw_result=raw_result,
            )
            log_error(
                logger,
                "cache_handle_unavailable_no_recreate",
                request_id=request_row.get("id"),
                session_id=request_row.get("session_id"),
                connection_id=connection_id,
                cache_operation_id=op_id,
                provider_request_id=provider_request_id,
                recreate_attempted=False,
            )
            return self._prepare_failure(
                plan,
                reason="cache_handle_unavailable",
                message="Provider cache create returned without a usable cache handle; recreation is disabled",
            )

        # Persist the returned handle immediately, before verification. If the
        # process dies now, the operation ledger retains enough evidence for a
        # future reconciler without blindly re-creating an orphan resource.
        recorded = await self.repo.record_cache_operation_observation(
            operation_id=op_id,
            lease_owner=op_owner,
            lease_epoch=op_epoch,
            raw_result=observation,
            provider_request_id=(
                str(observation.get("provider_request_id"))
                if observation.get("provider_request_id")
                else None
            ),
        )
        if not recorded:
            raise CacheDecisionError(
                "CACHE_OPERATION_FENCE_REJECTED",
                "Gemini CachedContent was created but its handle could not be fenced into the operation ledger",
            )

        try:
            verified = await adapter.get(handle=handle)
            if verified.get("expire_time"):
                observation["expire_time"] = verified.get("expire_time")
            observation["verified_by_get"] = True
        except ProviderHTTPError as exc:
            error_observation = provider_http_error_observation(exc)
            raw_result = {
                **self._safe_provider_observation(observation),
                "outcome": "cache_create_verify_failed",
                "verify_provider_http_error": error_observation,
                "recreate_attempted": False,
            }
            await self._record_error_observation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                raw_result=raw_result,
                provider_request_id=error_observation.get("request_id"),
            )
            await self.repo.finish_cache_operation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                state="unknown",
                raw_result=raw_result,
            )
            log_error(
                logger,
                "cache_create_verify_provider_http_error",
                request_id=request_row.get("id"),
                session_id=request_row.get("session_id"),
                connection_id=connection_id,
                cache_operation_id=op_id,
                provider_error=error_observation,
                cache_handle_present=True,
                recreate_attempted=False,
            )
            return self._provider_prepare_failure(plan, exc, "cache_create_unknown")
        except (httpx.HTTPError, RuntimeError) as exc:
            error_observation = safe_exception_observation(exc, phase="cache_create_verify")
            raw_result = {
                **self._safe_provider_observation(observation),
                "outcome": "cache_create_verify_failed",
                "verify_exception": error_observation,
                "recreate_attempted": False,
            }
            await self._record_error_observation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                raw_result=raw_result,
            )
            await self.repo.finish_cache_operation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                state="unknown",
                raw_result=raw_result,
            )
            log_error(
                logger,
                "cache_create_verify_exception_no_recreate",
                request_id=request_row.get("id"),
                session_id=request_row.get("session_id"),
                connection_id=connection_id,
                cache_operation_id=op_id,
                error=error_observation,
                cache_handle_present=True,
                recreate_attempted=False,
            )
            return self._provider_prepare_failure(plan, exc, "cache_create_unknown")

        spec_hash = stable_hash(spec)
        published = await self.repo.publish_cache_resource(
            operation_id=str(operation["op_id"]),
            lease_owner=str(operation.get("lease_owner") or fence.owner),
            lease_epoch=int(operation.get("lease_epoch") or 0),
            tenant_id=str(request_row["tenant_id"]),
            conversation_hash=str(request_row["conversation_hash"]),
            session_id=str(request_row["session_id"]),
            offering_id=str(snapshot.get("offering_id") or ""),
            connection_id=connection_id,
            scope_hash=scope_hash,
            content_fingerprint=content_fingerprint,
            reuse_key=reuse_key,
            prefix_version=prefix_version,
            token_count=token_count,
            spec_hash=spec_hash,
            provider_handle_ref=handle,
            expire_time=observation.get("expire_time"),
            profile_hash=str(plan.get("profile_hash") or ""),
            raw_result=observation,
        )
        if not published:
            raise CacheDecisionError(
                "CACHE_BINDING_FENCE_REJECTED",
                "Cache resource creation result was rejected by cache-operation fencing",
            )
        log_info(
            logger,
            "cache_resource_created",
            request_id=request_row.get("id"),
            session_id=request_row.get("session_id"),
            connection_id=connection_id,
            cache_operation_id=op_id,
            resource_id=published.get("id"),
            prefix_token_count=token_count,
            physical_plan_hash=spec.get("physical_plan_hash"),
        )
        return self._binding_from_resource(
            published,
            decision_reason="resource_created",
            cached_history_version=prefix_version,
            token_count=token_count,
            reuse_key=reuse_key,
            spec=spec,
            provider_observation=observation,
            growth_decision=prefix_growth_decision,
            creation_decision=cache_creation_decision,
        )

    async def _evaluate_prefix_growth(
        self,
        *,
        adapter: CacheResourceAdapter,
        existing: dict[str, Any],
        spec: dict[str, Any],
        cfg: dict[str, Any],
        request_row: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """Decide whether a longer immutable prefix has a positive cost case.

        No growth is performed without an explicit, frozen price schedule and
        expected future reuse count. Unknown prices/TTL economics are not treated
        as zero-cost assumptions.
        """
        policy = cfg.get("prefix_growth") if isinstance(cfg.get("prefix_growth"), dict) else {}
        auto_economics = cfg.get("auto_economics") if isinstance(cfg.get("auto_economics"), dict) else {}
        # Optional *frozen* history-growth policy for layout/9. Historical
        # outputs are eligible only when the native projector proves a safe
        # whole-turn prefix. Nothing is appended in-place to CachedContent.
        if policy.get("enabled") is not True and auto_economics.get("enable_history_growth") is True:
            prices = auto_economics.get("price_schedule") if isinstance(auto_economics.get("price_schedule"), dict) else {}
            prediction = auto_economics.get("reuse_prediction") if isinstance(auto_economics.get("reuse_prediction"), dict) else {}
            policy = {
                **prices,
                "enabled": True,
                "expected_future_reuses": prediction.get("expected_future_reuses", 0),
                "minimum_additional_tokens": auto_economics.get("minimum_additional_tokens", 1024),
                "safety_margin_ratio": auto_economics.get("safety_margin_ratio", .2),
            }
        if not policy or policy.get("enabled") is not True:
            return False, {"decision": "reuse", "reason": "growth_policy_disabled"}

        if auto_economics.get("new_economics_policy") in {"current_read/1", "relative_cache/2"}:
            # A previous model answer is not a reason to write a fresh snapshot
            # every turn. Growth is opt-in, requires a sizable complete-turn
            # delta, and compares marginal savings with the full new generation.
            return await self._evaluate_prefix_growth_current_read(
                adapter=adapter, existing=existing, spec=spec, policy=policy,
                economics=auto_economics,
            )

        def _number(name: str, *, allow_zero: bool = True) -> float | None:
            value = policy.get(name)
            if isinstance(value, bool):
                return None
            try:
                parsed = float(value)
            except Exception:
                return None
            if not math.isfinite(parsed) or parsed < 0 or (not allow_zero and parsed <= 0):
                return None
            return parsed

        expected_reuses = policy.get("expected_future_reuses")
        if isinstance(expected_reuses, bool):
            expected_reuses = None
        try:
            expected_reuses = int(expected_reuses)
        except Exception:
            expected_reuses = 0
        currency = str(policy.get("currency") or "").strip().upper()
        price_revision = str(policy.get("price_revision") or "").strip()
        input_rate = _number("input_cost_per_million_tokens", allow_zero=False)
        read_rate = _number("cache_read_cost_per_million_tokens")
        write_rate = _number("cache_write_cost_per_million_tokens")
        storage_rate = _number("storage_cost_per_million_tokens_minute")
        if (
            expected_reuses <= 0
            or not currency
            or not price_revision
            or input_rate is None
            or read_rate is None
            or write_rate is None
            or storage_rate is None
            or input_rate <= read_rate
        ):
            return False, {"decision": "reuse", "reason": "growth_cost_schedule_incomplete"}

        existing_tokens = existing.get("token_count")
        if isinstance(existing_tokens, bool):
            existing_tokens = None
        try:
            existing_tokens = int(existing_tokens)
        except Exception:
            existing_tokens = None
        if existing_tokens is None or existing_tokens < 0:
            return False, {"decision": "reuse", "reason": "existing_prefix_token_count_unknown"}

        try:
            measured_tokens = int(await adapter.measure(spec=spec))
        except Exception as exc:
            return False, {
                "decision": "reuse",
                "reason": "target_prefix_measurement_failed",
                "measurement_error_type": type(exc).__name__,
            }
        if measured_tokens < 0:
            return False, {"decision": "reuse", "reason": "target_prefix_measurement_invalid"}

        additional_tokens = max(0, measured_tokens - existing_tokens)
        minimum_additional = policy.get("minimum_additional_tokens", 1)
        if isinstance(minimum_additional, bool):
            minimum_additional = 1
        try:
            minimum_additional = max(1, int(minimum_additional))
        except Exception:
            minimum_additional = 1
        if additional_tokens < minimum_additional:
            return False, {
                "decision": "reuse",
                "reason": "additional_prefix_too_small",
                "existing_prefix_tokens": existing_tokens,
                "target_prefix_tokens": measured_tokens,
                "additional_prefix_tokens": additional_tokens,
                "minimum_additional_tokens": minimum_additional,
            }

        ttl_seconds = int(spec.get("ttl_seconds") or 3600)
        ttl_minutes = ttl_seconds / 60.0
        expected_savings = (
            expected_reuses * additional_tokens * (input_rate - read_rate) / 1_000_000.0
        )
        creation_cost = measured_tokens * write_rate / 1_000_000.0
        storage_cost = measured_tokens * ttl_minutes * storage_rate / 1_000_000.0
        total_creation_and_storage = creation_cost + storage_cost
        margin = _number("safety_margin_ratio")
        if margin is None:
            margin = 0.2 if auto_economics.get("enable_history_growth") is True else 0.0
        should_grow = expected_savings > total_creation_and_storage * (1.0 + min(margin, 5.0))
        return should_grow, {
            "decision": "grow" if should_grow else "reuse",
            "reason": (
                "expected_reuse_savings_exceed_create_and_ttl_storage_cost"
                if should_grow
                else "expected_reuse_savings_do_not_cover_create_and_ttl_storage_cost"
            ),
            "currency": currency,
            "price_revision": price_revision,
            "expected_future_reuses": expected_reuses,
            "existing_prefix_tokens": existing_tokens,
            "target_prefix_tokens": measured_tokens,
            "additional_prefix_tokens": additional_tokens,
            "ttl_seconds": ttl_seconds,
            "expected_reuse_savings": round(expected_savings, 10),
            "cache_creation_cost": round(creation_cost, 10),
            "ttl_storage_cost": round(storage_cost, 10),
            "expected_net_savings": round(expected_savings - total_creation_and_storage, 10),
        }

    async def _evaluate_prefix_growth_current_read(
        self, *, adapter: CacheResourceAdapter, existing: dict[str, Any],
        spec: dict[str, Any], policy: dict[str, Any], economics: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """6.1/6.2: incremental benefit; prior frozen policies remain unchanged."""
        try:
            existing_tokens = int(existing.get("token_count"))
            if existing_tokens <= 0:
                raise ValueError("missing existing token measurement")
        except (TypeError, ValueError, OverflowError):
            return False, {"decision": "reuse", "reason": "existing_prefix_token_count_unknown"}
        try:
            target_tokens = int(await adapter.measure(spec=spec))
        except Exception as exc:
            return False, {
                "decision": "reuse", "reason": "growth_target_measurement_failed",
                "measurement_error_type": type(exc).__name__,
            }
        if target_tokens <= existing_tokens:
            return False, {
                "decision": "reuse", "reason": "no_additional_cached_tokens",
                "existing_prefix_tokens": existing_tokens,
                "target_prefix_tokens": target_tokens,
            }
        minimum = policy.get("minimum_additional_tokens", 4096)
        if type(minimum) is not int or minimum <= 0:
            minimum = 4096
        if target_tokens - existing_tokens < minimum:
            return False, {
                "decision": "reuse", "reason": "additional_prefix_too_small",
                "existing_prefix_tokens": existing_tokens,
                "target_prefix_tokens": target_tokens,
                "minimum_additional_tokens": minimum,
            }
        if str(economics.get("new_economics_policy") or "") == "relative_cache/2":
            economic = evaluate_relative_cache_strategy(
                tokens=target_tokens,
                ttl_seconds=int(spec.get("ttl_seconds") or 3600),
                config=economics,
                forced=False,
                already_cached_tokens=existing_tokens,
            )
        else:
            economic = evaluate_current_read_resource(
                tokens=target_tokens,
                ttl_seconds=int(spec.get("ttl_seconds") or 3600),
                config=economics,
                forced=False,
                already_cached_tokens=existing_tokens,
            )
        should_grow = economic.get("allow") is True
        return should_grow, {
            "decision": "grow" if should_grow else "reuse",
            "reason": str(economic.get("reason") or "growth_economics_unavailable"),
            "existing_prefix_tokens": existing_tokens,
            "target_prefix_tokens": target_tokens,
            "additional_prefix_tokens": target_tokens - existing_tokens,
            "economics": economic,
        }

    async def _measure(
        self,
        *,
        adapter: CacheResourceAdapter,
        spec: dict[str, Any],
        plan: dict[str, Any],
        cfg: dict[str, Any],
        request_row: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> int | dict[str, Any]:
        try:
            token_count = await adapter.measure(spec=spec)
        except ProviderHTTPError as exc:
            error_observation = provider_http_error_observation(exc)
            log_error(
                logger,
                "cache_measurement_provider_http_error",
                request_id=request_row.get("id"),
                session_id=request_row.get("session_id"),
                connection_id=snapshot.get("connection_id"),
                provider_error=error_observation,
                physical_plan_hash=spec.get("physical_plan_hash"),
            )
            return self._provider_prepare_failure(plan, exc, "cache_measurement_failed")
        except (httpx.HTTPError, RuntimeError) as exc:
            log_error(
                logger,
                "cache_measurement_exception",
                request_id=request_row.get("id"),
                session_id=request_row.get("session_id"),
                connection_id=snapshot.get("connection_id"),
                error=safe_exception_observation(exc, phase="cache_measurement"),
                physical_plan_hash=spec.get("physical_plan_hash"),
            )
            return self._provider_prepare_failure(plan, exc, "cache_measurement_failed")

        minimum = cfg.get("minimum_cacheable_tokens")
        if isinstance(minimum, int) and token_count < minimum:
            return {
                "mechanism": None,
                "decision_reason": "below_minimum",
                "projection_version": str(spec.get("projection_version") or ""),
                "metadata": {
                    "provider_measurement": "countTokens",
                    "prefix_token_count": token_count,
                    "minimum_cacheable_tokens": minimum,
                    **self._spec_binding_metadata(spec),
                },
            }
        return token_count

    @staticmethod
    def _token_count_from_create_observation(observation: dict[str, Any]) -> int | None:
        usage = observation.get("usage_metadata")
        if not isinstance(usage, dict):
            return None
        value = usage.get("totalTokenCount")
        if isinstance(value, bool) or value is None:
            return None
        try:
            parsed = int(value)
        except Exception:
            return None
        return parsed if parsed >= 0 else None

    async def _record_error_observation(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        lease_epoch: int,
        raw_result: dict[str, Any],
        provider_request_id: str | None = None,
    ) -> None:
        try:
            recorded = await self.repo.record_cache_operation_observation(
                operation_id=operation_id,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
                raw_result=raw_result,
                provider_request_id=provider_request_id,
            )
        except Exception as ledger_exc:
            log_error(
                logger,
                "cache_operation_error_observation_write_failed",
                cache_operation_id=operation_id,
                error=safe_exception_observation(ledger_exc, phase="operation_ledger_observation"),
            )
            return
        if not recorded:
            log_warning(
                logger,
                "cache_operation_error_observation_fence_rejected",
                cache_operation_id=operation_id,
            )

    @staticmethod
    def _safe_provider_observation(observation: dict[str, Any]) -> dict[str, Any]:
        safe = dict(observation)
        if safe.get("provider_request_id"):
            safe["provider_request_id"] = pseudonymize_provider_request_id(
                str(safe.get("provider_request_id"))
            )
        return safe

    @staticmethod
    def _spec_binding_metadata(spec: dict[str, Any]) -> dict[str, Any]:
        values = {
            "physical_layout_version": spec.get("layout_version"),
            "projection_version": spec.get("projection_version"),
            "physical_plan_hash": spec.get("physical_plan_hash"),
            "cached_prefix_wire_hash": spec.get("cached_prefix_wire_hash"),
            "uncached_suffix_wire_hash": spec.get("uncached_suffix_wire_hash"),
            "uncached_suffix_wire_hashes": spec.get("uncached_suffix_wire_hashes"),
            "occurrence_mapping_hash": spec.get("occurrence_mapping_hash"),
            "cacheability_reason": spec.get("cacheability_reason"),
            "cache_create_request_bytes": spec.get("cache_create_request_bytes"),
        }
        return {key: value for key, value in values.items() if value not in (None, "")}

    def _observation_expiring(self, observation: dict[str, Any]) -> bool:
        value = observation.get("expire_time")
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return True
        safety = max(0, int(getattr(self.repo.settings, "cache_expiry_safety_seconds", 30)))
        return (parsed - datetime.now(timezone.utc)).total_seconds() <= safety

    @classmethod
    def _binding_from_resource(
        cls,
        resource: dict[str, Any],
        *,
        decision_reason: str,
        cached_history_version: int,
        token_count: Any,
        reuse_key: str,
        spec: dict[str, Any],
        provider_observation: dict[str, Any] | None = None,
        growth_decision: dict[str, Any] | None = None,
        creation_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        projection_version = str(spec.get("projection_version") or "")
        suffix_hashes = spec.get("uncached_suffix_wire_hashes") if isinstance(spec.get("uncached_suffix_wire_hashes"), dict) else {}
        selected_suffix_hash = suffix_hashes.get(str(cached_history_version)) or spec.get("uncached_suffix_wire_hash")
        metadata = {
            "cached_history_version": int(cached_history_version),
            "prefix_token_count": (int(token_count) if token_count is not None else None),
            "provider_measurement": (
                "cachedContents.create.usageMetadata"
                if str(spec.get("measurement_order") or "") == "provider_create"
                else "countTokens"
            ),
            "reuse_key_hash": stable_hash(reuse_key),
            "projection_version": projection_version,
            "provider_expire_time": (provider_observation or {}).get("expire_time"),
            **cls._spec_binding_metadata(spec),
        }
        if selected_suffix_hash not in (None, ""):
            metadata["uncached_suffix_wire_hash"] = selected_suffix_hash
        if growth_decision is not None:
            metadata["prefix_growth_decision"] = growth_decision
        if decision_reason.startswith("resource_reused") and creation_decision is None:
            metadata["cache_cost_decision"] = {
                "reason": "existing_compatible_resource_reused",
                "new_resource_created": False,
            }
        if creation_decision is not None:
            metadata["cache_cost_decision"] = creation_decision
        return {
            "mechanism": "stateful_resource",
            "decision_reason": decision_reason,
            "resource_id": resource.get("id"),
            "resource_generation": int(resource.get("generation") or 1),
            "provider_handle": resource.get("provider_handle_ref"),
            "expires_at": resource.get("expire_time"),
            "projection_version": projection_version or "relay-cache-projection/1",
            "prefix": {
                "prefix_version": int(cached_history_version),
                "content_fingerprint": str(resource.get("content_fingerprint") or ""),
            },
            "metadata": metadata,
        }

    @staticmethod
    def _prepare_failure(plan: dict[str, Any], *, reason: str, message: str) -> dict[str, Any]:
        if plan.get("requested_mode") == "auto" and plan.get("allow_uncached_same_context"):
            return {"mechanism": None, "decision_reason": reason}
        raise CacheDecisionError("CACHE_PREPARE_FAILED", message, reason=reason)

    def _provider_prepare_failure(self, plan: dict[str, Any], exc: Exception, reason: str) -> dict[str, Any]:
        if plan.get("requested_mode") == "auto" and plan.get("allow_uncached_same_context"):
            return {
                "mechanism": None,
                "decision_reason": reason,
                "metadata": {"prepare_error_type": type(exc).__name__},
            }
        raise CacheDecisionError(
            "CACHE_PREPARE_FAILED",
            f"Stateful cache preparation failed before model dispatch: {type(exc).__name__}",
            reason=reason,
        ) from exc
