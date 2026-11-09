from __future__ import annotations


import logging
from typing import Any

from app.protocol_adapters.base import ProviderRequestError
from app.observability import error as log_error


logger = logging.getLogger("model-relay-runtime")


from app.shared_contracts import ExecutionConflictError

def route_revision(session: dict[str, Any]) -> str | None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    route = metadata.get("_relay_route") if isinstance(metadata.get("_relay_route"), dict) else {}
    return str(route.get("route_revision")) if route.get("route_revision") else None


class ConditionVerifier:
    def __init__(self, repo, providers):
        self.repo, self.providers = repo, providers

    async def verify(self, plan):
        row, session, snapshot = plan.request_record, plan.session_record, plan.snapshot
        current = await self.repo.get_request(row["id"])
        if not current or current.get("provider_dispatch_state", "not_sent") != "not_sent":
            raise ExecutionConflictError("Only an unsent Request may enter the runtime")
        if current.get("status") in {"cancelled", "succeeded", "failed", "indeterminate"}:
            raise ExecutionConflictError("A terminal Request cannot enter the runtime")
        if snapshot.get("schema_version") in {"relay-request/2.3", "relay-request/2.4"} and plan.execution_fence is None:
            raise ProviderRequestError("CACHE_BINDING_FENCE_REJECTED", "Cache-aware Request has no execution fence")
        if plan.execution_fence is not None:
            from datetime import datetime, timezone
            fence = plan.execution_fence
            if current.get("job_id"):
                owner = await self.repo.get_job(current["job_id"])
                owner_key, epoch_key, expiry_key = "lease_owner", "lease_epoch", "lease_expires_at"
            else:
                owner = current
                owner_key, epoch_key, expiry_key = "request_executor_id", "request_executor_epoch", "request_executor_lease_expires_at"
            if not owner or owner.get(owner_key) != fence.owner or owner.get(epoch_key) != fence.epoch:
                raise ExecutionConflictError("Request execution fence is no longer owned by this executor")
            expiry = owner.get(expiry_key)
            if isinstance(expiry, str): expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if not isinstance(expiry, datetime) or expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise ExecutionConflictError("Request execution lease has expired")
        if snapshot.get("context_plan_hash") and snapshot["context_plan_hash"] != plan.context_plan.get("context_plan_hash"):
            raise ProviderRequestError("CACHE_CONTEXT_MISMATCH", "Frozen context identity differs from accepted Request")
        route = (session.get("metadata") or {}).get("_relay_route") or {}
        identity = route.get("connection_identity_hash")
        if identity and snapshot.get("connection_identity_hash") != identity:
            raise ProviderRequestError("CONNECTION_IDENTITY_MISMATCH", "Request connection differs from frozen Session")
        self._assert_frozen_route(session, snapshot, request_id=str(row["id"]))

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
                route_revision=route_revision(session),
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
            if str(snapshot.get("schema_version") or "") in {"relay-request/2.3", "relay-request/2.4"}:
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

