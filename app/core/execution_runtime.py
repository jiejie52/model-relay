from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..materials.resolver import MaterialResolver
from ..materials.binding_resolver import BindingResolver
from ..persistence.object_storage import ObjectLocation, StorageRegistry
from ..providers.registry import ProviderRegistry
from ..providers.base import ProviderRequestError
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
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.providers = providers
        self.materials = materials
        self.bindings = bindings
        self.settings = settings

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
            await self.repo.update_request(
                request_row["id"],
                {"material_binding_snapshot": material_bindings},
            )

        adapter = self.providers.get_v2(str(snapshot["connection_id"]))
        await self.repo.update_request(
            request_row["id"],
            {
                "provider_dispatch_state": "dispatch_started",
                "started_at": request_row.get("started_at") or utcnow().isoformat(),
            },
        )
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
        )
        try:
            result = await adapter.execute(
                V2ExecutionContext(
                    snapshot=snapshot,
                    session=session,
                    history=history,
                    material_ids=material_ids,
                    material_bindings=material_bindings,
                    tenant_id=request_row["tenant_id"],
                    conversation_hash=request_row["conversation_hash"],
                    request_id=request_id,
                    session_id=session_id,
                )
            )
        except Exception as exc:
            log_error(
                logger,
                "provider_call_failed",
                exc_info=True,
                request_id=request_id,
                session_id=session_id,
                provider=snapshot.get("provider"),
                connection_id=snapshot.get("connection_id"),
                model=snapshot.get("model"),
                adapter_version=getattr(adapter, "adapter_version", None),
                route_revision=self._route_revision(session),
                phase=getattr(exc, "phase", None) or "model_inference",
                duration_ms=elapsed_ms(provider_started_ms),
                failure_class=exception_failure_class(exc),
                upstream_http_status=getattr(exc, "status_code", None),
                upstream_request_id=getattr(exc, "request_id", None),
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
        await self.repo.update_request(
            request_row["id"],
            {
                "provisional_result_object_id": raw_object_id,
                "provisional_output_object_id": output_object_id,
                "provider_response_id": result.response_id,
            },
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

        adapter_meta = self.providers.describe(expected["connection_id"])
        if adapter_meta is None:
            raise ProviderRequestError(
                "ROUTE_ADAPTER_NOT_REGISTERED",
                "The Session's frozen Relay route has no registered inference adapter on this worker",
            )
        registered_provider = str(adapter_meta.get("provider") or "")
        # Old pre-route sessions do not carry _relay_route and may have legacy
        # provider labels. New route-frozen Sessions are strict.
        if route is not None and registered_provider and registered_provider != expected["provider"].lower():
            raise ProviderRequestError(
                "ROUTE_BINDING_MISMATCH",
                "The frozen Session provider does not match the registered Adapter",
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
