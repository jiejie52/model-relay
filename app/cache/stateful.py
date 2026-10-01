from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from ..core.idempotency import stable_hash
from ..providers.base import ProviderHTTPError
from .contracts import CacheDecisionError, ExecutionFence


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
        spec = adapter.build_spec(
            snapshot=snapshot,
            history=history,
            context_plan=context_plan,
            material_bindings=material_bindings,
            session=session,
            ttl_seconds=(int(cfg["ttl_seconds"]) if isinstance(cfg.get("ttl_seconds"), int) else None),
        )
        if not bool(spec.get("cacheable")):
            return {
                "mechanism": None,
                "decision_reason": "no_cacheable_prefix",
                "metadata": {"provider_measurement": "not_applicable"},
            }

        content_fingerprint = str(spec.get("content_fingerprint") or "")
        reuse_key = str(spec.get("reuse_key") or "")
        prefix_version = int(spec.get("prefix_version") or 0)
        compatible = spec.get("compatible_prefix_fingerprints") if isinstance(spec.get("compatible_prefix_fingerprints"), dict) else {}

        # Reuse is compatible-prefix based, not exact-current-history based.  A
        # resource created for committed turn N can serve turn N+1 and later as
        # long as that old prefix is byte-semantically identical.
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
            try:
                observation = await adapter.get(handle=handle)
            except ProviderHTTPError as exc:
                if exc.status_code in {404, 410}:
                    await self.repo.invalidate_cache_resource(
                        resource_id=str(existing["id"]),
                        provider_handle_ref=handle,
                        state="expired" if exc.status_code == 410 else "invalid",
                    )
                    continue
                return self._provider_prepare_failure(plan, exc, "cache_resource_probe_failed")
            except (httpx.HTTPError, RuntimeError) as exc:
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
                token_count=existing.get("token_count"),
                reuse_key=reuse_key,
                projection_version=str(spec.get("projection_version") or ""),
                provider_observation=observation,
            )

        # The Provider is authoritative for threshold measurement.  This guard
        # runs before any cache create side effect and therefore may resolve an
        # accepted 'on' request to None when the context is below minimum.
        try:
            token_count = await adapter.measure(spec=spec)
        except ProviderHTTPError as exc:
            return self._provider_prepare_failure(plan, exc, "cache_measurement_failed")
        except (httpx.HTTPError, RuntimeError) as exc:
            return self._provider_prepare_failure(plan, exc, "cache_measurement_failed")

        minimum = cfg.get("minimum_cacheable_tokens")
        if isinstance(minimum, int) and token_count < minimum:
            return {
                "mechanism": None,
                "decision_reason": "below_minimum",
                "metadata": {
                    "provider_measurement": "countTokens",
                    "prefix_token_count": token_count,
                    "minimum_cacheable_tokens": minimum,
                },
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
            # A different executor may have just published the singleflight
            # result.  Re-read once before deciding this prepare attempt failed.
            raced = await self.repo.find_compatible_cache_resources(
                scope_hash=scope_hash,
                reuse_key=reuse_key,
                max_prefix_version=prefix_version,
            )
            for row in raced:
                if str(row.get("content_fingerprint") or "") == content_fingerprint:
                    return self._binding_from_resource(
                        row,
                        decision_reason="resource_reused_after_race",
                        cached_history_version=int(row.get("prefix_version") or prefix_version),
                        token_count=row.get("token_count"),
                        reuse_key=reuse_key,
                        projection_version=str(spec.get("projection_version") or ""),
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

        # From this point onward the Provider-side create may have happened.  An
        # expired cache-operation lease is therefore never a license to POST the
        # same create again.  This is deliberately independent from the model
        # Request's provider_dispatch_state, which is still not_sent here.
        try:
            observation = await adapter.create(spec=spec, operation=operation)
        except ProviderHTTPError as exc:
            state = "failed" if 400 <= int(exc.status_code) < 500 else "unknown"
            await self.repo.finish_cache_operation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                state=state,
                raw_result={"status_code": exc.status_code, "phase": exc.phase},
            )
            return self._provider_prepare_failure(
                plan,
                exc,
                "cache_create_rejected" if state == "failed" else "cache_create_unknown",
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            await self.repo.finish_cache_operation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                state="unknown",
                raw_result={"exception_type": type(exc).__name__, "phase": "cache_create"},
            )
            return self._provider_prepare_failure(plan, exc, "cache_create_unknown")

        # Persist the returned handle immediately, before any verification call.
        # If the process dies after Provider create, the operation ledger retains
        # enough evidence for reconciliation instead of blindly re-creating an
        # orphan resource.
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
            # Do not publish an unverified Provider handle into the reusable
            # resource ledger. A failure here occurs *after* create side effect,
            # therefore it is always unknown rather than a retryable failed create.
            verified = await adapter.get(handle=str(observation.get("handle") or ""))
            if verified.get("expire_time"):
                observation["expire_time"] = verified.get("expire_time")
            observation["verified_by_get"] = True
        except (ProviderHTTPError, httpx.HTTPError, RuntimeError) as exc:
            await self.repo.finish_cache_operation(
                operation_id=op_id,
                lease_owner=op_owner,
                lease_epoch=op_epoch,
                state="unknown",
                raw_result={
                    **observation,
                    "verify_error_type": type(exc).__name__,
                    "verify_status_code": (exc.status_code if isinstance(exc, ProviderHTTPError) else None),
                    "phase": "cache_create_verify",
                },
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
            provider_handle_ref=str(observation.get("handle") or ""),
            expire_time=observation.get("expire_time"),
            profile_hash=str(plan.get("profile_hash") or ""),
            raw_result=observation,
        )
        if not published:
            raise CacheDecisionError(
                "CACHE_BINDING_FENCE_REJECTED",
                "Cache resource creation result was rejected by cache-operation fencing",
            )
        return self._binding_from_resource(
            published,
            decision_reason="resource_created",
            cached_history_version=prefix_version,
            token_count=token_count,
            reuse_key=reuse_key,
            projection_version=str(spec.get("projection_version") or ""),
            provider_observation=observation,
        )

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

    @staticmethod
    def _binding_from_resource(
        resource: dict[str, Any],
        *,
        decision_reason: str,
        cached_history_version: int,
        token_count: Any,
        reuse_key: str,
        projection_version: str,
        provider_observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "mechanism": "stateful_resource",
            "decision_reason": decision_reason,
            "resource_id": resource.get("id"),
            "resource_generation": int(resource.get("generation") or 1),
            "provider_handle": resource.get("provider_handle_ref"),
            "expires_at": resource.get("expire_time"),
            "metadata": {
                "cached_history_version": int(cached_history_version),
                "prefix_token_count": (int(token_count) if token_count is not None else None),
                "reuse_key_hash": stable_hash(reuse_key),
                "projection_version": projection_version,
                "provider_expire_time": (provider_observation or {}).get("expire_time"),
            },
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
