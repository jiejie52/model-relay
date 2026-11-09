from __future__ import annotations


import json
import logging
from typing import Any

from app.core.context_codec import canonical_structure, freeze_history_turn
from app.core.idempotency import stable_hash
from app.protocol_adapters.base import ProviderRequestError
from app.observability import elapsed_ms, error as log_error, info as log_info, now_ms
from app.execution_plane.structured_output import StructuredOutputError, resolve_structured_output, validate_against_schema
from app.utils import json_bytes, truncate_utf8, utcnow


logger = logging.getLogger("model-relay-runtime")


from app.shared_contracts import ExecutionConflictError, thaw_json
from app.runtime.contracts import ActualExecutionFact
from app.persistence.execution_artifacts import ExecutionArtifacts

from app.protocol_adapters.v2_base import StandardizedProviderResult
from app.runtime.execution_guard import ensure_execution_allowed
from app.execution_plane.result_contract import validate_execution_observations

class ResultCommitter:
    def __init__(self, repo, storage, settings, errors=None):
        self.repo, self.settings = repo, settings
        self.artifacts = ExecutionArtifacts(repo, storage, settings)
        self.errors = errors

    async def finalize(self, plan, fact):
        await ensure_execution_allowed()
        if fact.request_id != str(plan.request_record["id"]):
            raise ExecutionConflictError("Execution fact belongs to another Request")
        if fact.raw_error_envelope is not None:
            return await self.fail_fact(plan, fact)
        if fact.provider_dispatch_state != "result_stored" or fact.standardized_result is None:
            raise ExecutionConflictError("Result commit requires an archived provider response")
        request_row, session, snapshot = map(thaw_json, (plan.request_record, plan.session_record, plan.snapshot))
        request_id, session_id = str(request_row["id"]), str(request_row["session_id"])
        result = fact.standardized_result
        contract_observation = None
        if snapshot.get("schema_version") == "relay-request/2.4":
            try:
                contract_observation = validate_execution_observations(snapshot, result.observed or {})
            except ProviderRequestError as exc:
                setattr(exc, "provider_success_object_id", fact.provisional_raw_object_id)
                setattr(exc, "provider_output_object_id", fact.provisional_output_object_id)
                raise
        history = thaw_json(plan.history)
        observation = thaw_json(fact.execution_observation)
        wire = {"normalize": observation.get("normalize", False)}
        upgrade = snapshot.get("cache_upgrade") or {}
        cumulative_history = snapshot.get("history_mode", session.get("history_mode", "cumulative")) != "ephemeral_turn"
        fence = plan.execution_fence
        lease_owner, lease_epoch = (fence.owner, fence.epoch) if fence else (None, None)
        is_v3 = snapshot.get("schema_version") in {"relay-request/2.3", "relay-request/2.4"}
        raw_object_id, output_object_id = fact.provisional_raw_object_id, fact.provisional_output_object_id
        execution_started_ms = now_ms()
        self._check_observed_contract(
            snapshot,
            result.observed or {},
            request_id=request_id,
            session_id=session_id,
            raw_object_id=raw_object_id,
            output_object_id=output_object_id,
        )

        spec = resolve_structured_output(snapshot, fallback_name="structured_output")
        if (spec is not None and snapshot.get("structured_output_guarantee") in {"native_constrained", "native_json_schema"}
                and (result.observed or {}).get("json_repaired") is True):
            exc = StructuredOutputError("STRUCTURED_OUTPUT_NATIVE_CONSTRAINT_LOST",
                "The frozen native-constrained contract cannot be satisfied by gateway-repaired JSON")
            setattr(exc, "provider_success_object_id", raw_object_id)
            setattr(exc, "provider_output_object_id", output_object_id)
            raise exc
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
            full_text_object_id = await self.artifacts.store_object(
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
        if observation.get("cache_summary") is not None:
            compact_result["cache"] = observation["cache_summary"]
        if contract_observation is not None:
            compact_result["contract_observation"] = contract_observation
        if len(json_bytes(compact_result)) > self.settings.relay_result_hard_limit_bytes:
            compact_result["text"] = truncate_utf8(
                str(compact_result.get("text") or ""),
                min(self.settings.relay_result_preview_bytes, 196608),
            )
            compact_result["text_truncated"] = True

        history_object_id = None
        if cumulative_history and session.get("context_policy") == "conversation":
            new_history = list(history)
            entry = {
                "request_id": request_row["id"],
                "created_at": observation.get("recorded_at") or utcnow().isoformat(),
            }
            entry.update(
                canonical_structure(result.history_entry) if wire.get("normalize") else result.history_entry
            )
            if int(upgrade.get("version") or 0) >= 3 and wire.get("normalize"):
                entry = freeze_history_turn(entry)
            new_history.append(entry)
            history_object_id = await self.artifacts.store_history(
                request_row,
                new_history,
                int(request_row["expected_history_version"]) + 1,
            )

        if is_v3:
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
            raise ExecutionConflictError(
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
        allowed_models = list((snapshot.get("execution_authorization") or {}).get("allowed_actual_models") or [])
        if snapshot.get("upstream_model"): allowed_models.append(snapshot["upstream_model"])
        allowed = {expected_model.lower(), *(str(v).lower() for v in allowed_models)}
        mismatch = bool(expected_model and actual_model and actual_model.lower() not in allowed)
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

    @staticmethod
    def _route_revision(session: dict[str, Any]) -> str | None:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        route = metadata.get("_relay_route") if isinstance(metadata.get("_relay_route"), dict) else {}
        return str(route.get("route_revision")) if route.get("route_revision") else None


    async def recover_and_commit(self, plan, fence=None):
        row = plan.request_record
        if row.get("provider_dispatch_state") != "result_stored":
            raise ExecutionConflictError("Recovery requires result_stored")
        marker = row.get("provisional_compact_result") or {}
        object_id = marker.get("_relay_execution_fact")
        active_fence = fence or plan.execution_fence
        if object_id:
            identity = {"tenant_id": row["tenant_id"], "conversation_hash": row["conversation_hash"]}
            archive = await self.artifacts.load_json(object_id, **identity)
            if (archive.get("schema_version") != "relay-execution-fact/1" or archive.get("request_id") != str(row["id"])
                    or archive.get("snapshot_hash") != stable_hash(plan.snapshot)):
                raise ExecutionConflictError("Stored execution fact does not match frozen Request")
            raw = await self.artifacts.load_bytes(row["provisional_result_object_id"], **identity)
            if __import__("hashlib").sha256(raw).hexdigest() != archive.get("raw_sha256"):
                raise ExecutionConflictError("Archived raw response integrity mismatch")
            result = StandardizedProviderResult(raw_bytes=raw, **archive["result"])
            fact = ActualExecutionFact(str(row["id"]), "result_stored", standardized_result=result,
                provisional_raw_object_id=row["provisional_result_object_id"],
                provisional_output_object_id=row.get("provisional_output_object_id"),
                execution_observation=archive["observation"])
            return await self.finalize(plan, fact)
        # Historical requests already contain their validated compact result and
        # history object. Preserve that frozen recovery format unchanged.
        kwargs = dict(request_id=str(row["id"]), session_id=str(row["session_id"]),
            history_object_id=row.get("provisional_history_object_id"), result_object_id=row["provisional_result_object_id"],
            output_object_id=row.get("provisional_output_object_id"), compact_result=thaw_json(marker),
            provider_response_id=row.get("provider_response_id"), expected_history_version=int(row["expected_history_version"]))
        if row.get("request_identity_version") in {"relay-request/2.3", "relay-request/2.4"}:
            ok = await self.repo.complete_request_v3(**kwargs, fence_owner=active_fence.owner, fence_epoch=active_fence.epoch)
        else:
            ok = await self.repo.complete_request(**kwargs, lease_owner=active_fence.owner if active_fence else None,
                                                   lease_epoch=active_fence.epoch if active_fence else None)
        if not ok: raise ExecutionConflictError("Stored result could not be atomically committed")
        return thaw_json(marker)

    async def fail_fact(self, plan, fact):
        row, fence = plan.request_record, plan.execution_fence
        error = thaw_json(fact.raw_error_envelope or {})
        unknown = bool(error.get("unknown_completion"))
        if fact.provider_dispatch_state == "result_stored":
            # A persistence/observation error after raw facts were stored cannot
            # invalidate them. Leave recovery to the execution plane.
            return None
        kwargs = dict(request_id=str(row["id"]), session_id=str(row["session_id"]), error=error,
                      status="indeterminate" if unknown else "failed", release_session=not unknown)
        if row.get("request_identity_version") in {"relay-request/2.3", "relay-request/2.4"}:
            return await self.repo.fail_request_v3(**kwargs, fence_owner=fence.owner, fence_epoch=fence.epoch)
        return await self.repo.fail_request(**kwargs, lease_owner=fence.owner if fence else None,
                                          lease_epoch=fence.epoch if fence else None)
