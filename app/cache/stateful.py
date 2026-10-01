from __future__ import annotations

from typing import Any, Protocol

from .contracts import CacheDecisionError, ExecutionFence


class CacheResourceAdapter(Protocol):
    adapter_version: str

    async def create(self, *, spec: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]: ...
    async def get(self, *, handle: str, operation: dict[str, Any]) -> dict[str, Any]: ...
    async def renew(self, *, handle: str, expire_at: str, operation: dict[str, Any]) -> dict[str, Any]: ...
    async def delete(self, *, handle: str, operation: dict[str, Any]) -> dict[str, Any]: ...


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
    ) -> dict[str, Any]:
        connection_id = str(snapshot.get("connection_id") or "")
        adapter = self.registry.maybe_get(connection_id) if self.registry is not None else None
        if adapter is None:
            if plan.get("requested_mode") == "auto" and plan.get("allow_uncached_same_context"):
                return {"mechanism": None, "decision_reason": "stateful_adapter_unavailable"}
            raise CacheDecisionError(
                "CACHE_PREPARE_FAILED",
                "Stateful cache resource adapter is not registered for the frozen connection",
                reason="resource_adapter_unavailable",
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
        content_fingerprint = str(context_plan.get("stable_prefix_fingerprint") or "")
        existing = await self.repo.find_ready_cache_resource(
            scope_hash=scope_hash,
            content_fingerprint=content_fingerprint,
        )
        if existing:
            return {
                "mechanism": "stateful_resource",
                "decision_reason": "resource_reused",
                "resource_id": existing.get("id"),
                "resource_generation": int(existing.get("generation") or 1),
                "provider_handle": existing.get("provider_handle_ref"),
                "expires_at": existing.get("expire_time"),
            }

        operation = await self.repo.create_cache_operation_intent(
            request_id=str(request_row["id"]),
            scope_hash=scope_hash,
            content_fingerprint=content_fingerprint,
            operation_type="create",
            lease_owner=fence.owner,
            lease_epoch=fence.epoch,
        )
        spec = {
            "schema_version": "relay-cache-spec/1",
            "scope_hash": scope_hash,
            "content_fingerprint": content_fingerprint,
            "model": snapshot.get("model"),
            "connection_id": connection_id,
            "context_plan": context_plan,
            "material_bindings": material_bindings,
            "ttl_seconds": cfg.get("ttl_seconds"),
        }
        observation = await adapter.create(spec=spec, operation=operation)
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
            provider_handle_ref=str(observation.get("handle") or ""),
            expire_time=observation.get("expire_time"),
            profile_hash=str(plan.get("profile_hash") or ""),
            raw_result=observation,
        )
        if not published:
            raise CacheDecisionError(
                "CACHE_BINDING_FENCE_REJECTED",
                "Cache resource creation result was rejected by fencing",
            )
        return {
            "mechanism": "stateful_resource",
            "decision_reason": "resource_created",
            "resource_id": published.get("id"),
            "resource_generation": int(published.get("generation") or 1),
            "provider_handle": published.get("provider_handle_ref"),
            "expires_at": published.get("expire_time"),
        }
