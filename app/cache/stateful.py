from __future__ import annotations

from datetime import datetime, timezone
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
            return {
                "mechanism": None,
                "decision_reason": "no_cacheable_prefix",
                "metadata": {
                    "provider_measurement": "not_applicable",
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

        # Legacy layouts may pre-measure the exact frozen cached prefix. Layout/4
        # deliberately follows the AIHubMix/Google Gen AI SDK flow and skips this
        # step: CachedContent.create is the provider-authoritative threshold gate.
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
            return self._binding_from_resource(
                existing,
                decision_reason="resource_reused",
                cached_history_version=existing_version,
                token_count=token_count if token_count is not None else existing.get("token_count"),
                reuse_key=reuse_key,
                spec=spec,
                provider_observation=observation,
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
        )

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
            "occurrence_mapping_hash": spec.get("occurrence_mapping_hash"),
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
    ) -> dict[str, Any]:
        projection_version = str(spec.get("projection_version") or "")
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
