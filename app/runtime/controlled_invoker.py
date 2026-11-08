from __future__ import annotations

from dataclasses import replace

import logging
import math
from typing import Any

from app.observability.http_wire_logger import wire_context
from app.protocol_adapters.usage_normalizer import normalize_observed_cache_audit
from app.protocol_adapters.cache_usage import normalize_cache_usage
from app.core.idempotency import stable_hash
from app.protocol_adapters.error_observation import provider_http_error_observation
from app.protocol_adapters.base import ProviderHTTPError
from app.observability import elapsed_ms, error as log_error, info as log_info, now_ms, exception_failure_class, warning as log_warning
from app.utils import json_bytes, utcnow


logger = logging.getLogger("model-relay-runtime")


from app.shared_contracts import ExecutionConflictError
from app.runtime.contracts import ActualExecutionFact

from app.runtime.condition_verifier import route_revision
from app.runtime.execution_guard import ensure_execution_allowed

class ControlledInvoker:
    def __init__(self, repo, providers, artifacts, cache, settings):
        self.repo, self.providers, self.artifacts, self.cache, self.settings = repo, providers, artifacts, cache, settings

    async def invoke_once_and_record(self, state):
        await ensure_execution_allowed(states=("dispatch_started",))
        state.sealed_bundle.verify_integrity()
        affinity_headers = {key: state.wire["affinity_token"] for key in (
            state.wire.get("affinity_header"), state.wire.get("secondary_affinity_header"))
            if key and state.wire.get("affinity_token")}
        if affinity_headers != state.sealed_bundle.affinity_headers:
            raise ExecutionConflictError("Transport affinity differs from the sealed dispatch inputs")
        adapter = self.providers.get_v2(str(state.snapshot["connection_id"]))
        provider_started_ms = now_ms()
        metadata = state.snapshot.get("metadata") if isinstance(state.snapshot.get("metadata"), dict) else {}
        input_value = state.snapshot.get("input") if isinstance(state.snapshot.get("input"), dict) else {}
        business_stage = metadata.get("stage") or metadata.get("purpose") or input_value.get("stage")
        log_info(
            logger,
            "provider_call_started",
            request_id=state.request_id,
            session_id=state.session_id,
            provider=state.snapshot.get("provider"),
            connection_id=state.snapshot.get("connection_id"),
            model=state.snapshot.get("model"),
            adapter_version=getattr(adapter, "adapter_version", None),
            route_revision=route_revision(state.session),
            phase="model_inference",
            business_stage=business_stage,
            material_count=len(state.material_ids),
            cache_mechanism=(state.cache_binding or {}).get("mechanism"),
        )
        try:
            state.wire["phase"] = "inference"
            with wire_context(state.wire):
                result = await adapter.execute(
                    state.sealed_bundle
                )
        except Exception as exc:
            if state.wire.get("affinity_token") and state.fence is not None and isinstance(exc, ProviderHTTPError):
                route_observation = state.wire.get("route_observation") or {}
                expires = state.cache_binding_obj.expires_at if state.cache_binding else None
                handle_rejected = (
                    (state.cache_binding or {}).get("mechanism") == "stateful_resource"
                    and exc.status_code in {403, 404}
                    and bool(expires) and not self.cache.stateful._observation_expiring({"expire_time": expires})
                )
                if route_observation or handle_rejected:
                    try:
                        await self.repo.record_cache_affinity_event(state.request_id, {
                            "affinity_epoch": state.wire["affinity_epoch"], "route": route_observation,
                            "handle_rejected_before_expiry": bool(handle_rejected),
                        }, state.fence)
                    except Exception as audit_exc:
                        log_warning(logger, "cache_affinity_audit_failed", request_id=state.request_id,
                                    error_type=type(audit_exc).__name__)
            provider_error = (
                provider_http_error_observation(exc)
                if isinstance(exc, ProviderHTTPError)
                else None
            )
            log_error(
                logger,
                "provider_call_failed",
                exc_info=(False if provider_error is not None else True),
                request_id=state.request_id,
                session_id=state.session_id,
                provider=state.snapshot.get("provider"),
                connection_id=state.snapshot.get("connection_id"),
                model=state.snapshot.get("model"),
                adapter_version=getattr(adapter, "adapter_version", None),
                route_revision=route_revision(state.session),
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
            request_id=state.request_id,
            session_id=state.session_id,
            provider=state.snapshot.get("provider"),
            connection_id=state.snapshot.get("connection_id"),
            model=state.snapshot.get("model"),
            adapter_version=getattr(adapter, "adapter_version", None),
            route_revision=route_revision(state.session),
            phase="model_inference",
            duration_ms=elapsed_ms(provider_started_ms),
            http_status=result.http_status,
            upstream_request_id=result.provider_request_id,
            provider_response_id=result.response_id,
            response_bytes=len(result.raw_bytes),
        )

        raw_object_id = await self.artifacts.store_object(
            state.request_row,
            "raw-response.json",
            result.raw_bytes,
            "application/json",
        )
        output_object_id = await self.artifacts.store_object(
            state.request_row,
            "response-output.json",
            json_bytes(result.response_output),
            "application/json",
        )
        # Persist the upstream success body before any Relay parser/schema gate.
        # If validation fails, the original 2xx entity remains available for
        # diagnosis instead of being replaced by a synthetic Relay error.
        if not state.is_v3 and state.fence is None:
            await self.repo.update_request(
                state.request_row["id"],
                {
                    "provisional_result_object_id": raw_object_id,
                    "provisional_output_object_id": output_object_id,
                    "provider_response_id": result.response_id,
                },
            )

        cache_summary = None
        cache_usage = None
        if state.is_v3:
            plan = state.snapshot.get("cache_plan") if isinstance(state.snapshot.get("cache_plan"), dict) else {}
            usage_observation = normalize_cache_usage(
                protocol=(str(state.snapshot.get("protocol")) if state.snapshot.get("protocol") else None),
                usage=result.usage,
                effective_mechanism=(state.cache_binding or {}).get("mechanism"),
                requested_mode=str(state.snapshot.get("requested_cache_mode") or "auto"),
            )
            cache_usage = usage_observation.canonical()
            cache_prefix = (state.cache_binding or {}).get("prefix") if isinstance((state.cache_binding or {}).get("prefix"), dict) else {}
            cache_metadata = (state.cache_binding or {}).get("metadata") if isinstance((state.cache_binding or {}).get("metadata"), dict) else {}
            cache_mechanism = (state.cache_binding or {}).get("mechanism")
            cache_decision_reason = (state.cache_binding or {}).get("decision_reason") or plan.get("decision_reason")
            if cache_mechanism == "stateful_resource":
                if str(cache_decision_reason or "").startswith("resource_created"):
                    cache_resource_action = "created"
                elif "reused" in str(cache_decision_reason or ""):
                    cache_resource_action = "reused"
                else:
                    cache_resource_action = str(cache_decision_reason or "stateful_resource")
            elif cache_mechanism == "implicit_prefix":
                cache_resource_action = "implicit_prefix_projected"
            else:
                cache_resource_action = "none"
            cache_cost_observation: dict[str, Any] = {
                "status": "unavailable",
                "reason": "provider_usage_or_frozen_price_schedule_incomplete",
            }
            cache_plan_config = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
            if plan.get("schema_version") == "relay-unified-cache-plan/1":
                cache_plan_config = ((plan.get("candidates") or {}).get(cache_mechanism) or {}).get("mechanism_config") or {}
                cache_usage["arbitration"] = cache_metadata.get("arbitration")
            price_schedule = cache_plan_config.get("prefix_growth") if isinstance(cache_plan_config.get("prefix_growth"), dict) else {}
            economics_for_observation = cache_plan_config.get("auto_economics") if isinstance(cache_plan_config.get("auto_economics"), dict) else {}
            resolver_version = str(plan.get("resolver_version") or "")
            new_cost_observation = (
                (resolver_version == "relay-cache-resolver/5"
                 and economics_for_observation.get("new_economics_policy") == "current_read/1")
                or (resolver_version in {"relay-cache-resolver/6", "relay-cache-resolver/7"}
                    and economics_for_observation.get("new_economics_policy") == "relative_cache/2")
            )
            if new_cost_observation:
                price_schedule = (
                    economics_for_observation.get("price_schedule")
                    if isinstance(economics_for_observation.get("price_schedule"), dict)
                    else {}
                )
            price_currency = str(price_schedule.get("currency") or "").strip().upper()
            price_revision = str(price_schedule.get("price_revision") or "").strip()
            prompt_total = cache_usage.get("prompt_tokens_total")
            cache_read_tokens = cache_usage.get("cache_read_tokens")
            uncached_input_tokens = cache_usage.get("uncached_input_tokens")
            try:
                input_rate = float(price_schedule.get("input_cost_per_million_tokens"))
                if resolver_version in {"relay-cache-resolver/6", "relay-cache-resolver/7"} and economics_for_observation.get("new_economics_policy") == "relative_cache/2":
                    read_rate = input_rate * float(economics_for_observation.get("cache_read_input_ratio", 0.1))
                else:
                    read_rate = float(price_schedule.get("cache_read_cost_per_million_tokens"))
                if (
                    price_currency
                    and price_revision
                    and prompt_total is not None
                    and cache_read_tokens is not None
                    and uncached_input_tokens is not None
                    and math.isfinite(input_rate)
                    and math.isfinite(read_rate)
                    and input_rate >= 0
                    and read_rate >= 0
                ):
                    input_cost = float(uncached_input_tokens) * input_rate / 1_000_000.0
                    read_cost = float(cache_read_tokens) * read_rate / 1_000_000.0
                    write_cost = 0.0
                    storage_cost = 0.0
                    complete = True
                    if cache_resource_action == "created":
                        write_rate = price_schedule.get("cache_write_cost_per_million_tokens")
                        storage_rate = price_schedule.get("storage_cost_per_million_tokens_minute")
                        prefix_tokens = cache_metadata.get("prefix_token_count")
                        ttl_seconds = cache_metadata.get("ttl_seconds") or cache_plan_config.get("ttl_seconds")
                        if (resolver_version in {"relay-cache-resolver/6", "relay-cache-resolver/7"}
                            and economics_for_observation.get("new_economics_policy") == "relative_cache/2"):
                            write_rate = input_rate * float(economics_for_observation.get("explicit_create_input_ratio", 1.0))
                        elif (new_cost_observation and write_rate is None
                            and economics_for_observation.get("no_separate_creation_fee_verified") is True):
                            write_rate = 0.0
                        if (write_rate is None or prefix_tokens is None
                            or (not new_cost_observation and (storage_rate is None or ttl_seconds is None))):
                            complete = False
                        else:
                            write_rate_value = float(write_rate)
                            storage_rate_value = float(storage_rate) if storage_rate is not None else None
                            if (
                                not math.isfinite(write_rate_value) or write_rate_value < 0
                                or (storage_rate_value is not None and
                                    (not math.isfinite(storage_rate_value) or storage_rate_value < 0))
                            ):
                                complete = False
                            else:
                                write_cost = float(prefix_tokens) * write_rate_value / 1_000_000.0
                                storage_cost = (
                                    float(prefix_tokens) * (float(ttl_seconds) / 60.0)
                                    * storage_rate_value / 1_000_000.0
                                    if storage_rate_value is not None and ttl_seconds is not None
                                    else None
                                )
                    if complete:
                        cache_cost_observation = {
                            "status": "estimated_from_frozen_price_schedule",
                            "currency": price_currency,
                            "price_revision": price_revision,
                            "scope": "input_and_cache_only_excludes_output_tokens",
                            "uncached_input_cost": round(input_cost, 10),
                            "cache_read_cost": round(read_cost, 10),
                            "cache_write_cost": round(write_cost, 10),
                            "ttl_storage_cost": round(storage_cost, 10) if storage_cost is not None else None,
                            "total_estimated_cost": (
                                None if new_cost_observation else
                                round(input_cost + read_cost + write_cost + storage_cost, 10)
                            ),
                            "total_estimated_cost_excluding_storage": (
                                round(input_cost + read_cost + write_cost, 10)
                                if new_cost_observation else None
                            ),
                            "storage_excluded_from_optimization": new_cost_observation,
                            "pricing_basis": (
                                "relative_input_equivalent_policy"
                                if resolver_version in {"relay-cache-resolver/6", "relay-cache-resolver/7"}
                                and economics_for_observation.get("new_economics_policy") == "relative_cache/2"
                                else "frozen_price_schedule"
                            ),
                            "cache_read_input_ratio": (
                                economics_for_observation.get("cache_read_input_ratio", 0.1)
                                if resolver_version in {"relay-cache-resolver/6", "relay-cache-resolver/7"}
                                and economics_for_observation.get("new_economics_policy") == "relative_cache/2"
                                else None
                            ),
                            "explicit_create_input_ratio": (
                                economics_for_observation.get("explicit_create_input_ratio", 1.0)
                                if resolver_version in {"relay-cache-resolver/6", "relay-cache-resolver/7"}
                                and economics_for_observation.get("new_economics_policy") == "relative_cache/2"
                                else None
                            ),
                        }
            except (TypeError, ValueError, OverflowError):
                pass
            if str(state.snapshot.get("provider") or "").lower() == "gemini":
                binding_prefix_token_count = cache_metadata.get("prefix_token_count")
                if binding_prefix_token_count is None and isinstance(state.cache_binding_obj.prefix, dict):
                    binding_prefix_token_count = state.cache_binding_obj.prefix.get("prefix_token_count")
                cache_usage.update(
                    {
                        "material_reuse": state.material_reuse_observation,
                        "material_fingerprint": ((state.context_plan or {}).get("material_reuse") or {}).get("ordered_material_fingerprint"),
                        "planned_mechanism": plan.get("planned_mechanism"),
                        "implicit_prefix_fingerprint": (state.provider_physical_plan or {}).get("implicit_prefix_fingerprint"),
                        "payload_hash": state.wire.get("payload_sha256"),
                        "admission_status": cache_metadata.get("admission_status"),
                        "cache_resource_action": cache_resource_action,
                        "effective_cache_mechanism": cache_mechanism,
                        "reuse_evidence_key": state.snapshot.get("reuse_evidence_key"),
                        "cache_prefix_token_count": binding_prefix_token_count,
                        "cache_ttl_seconds": cache_metadata.get("ttl_seconds") or cache_plan_config.get("ttl_seconds"),
                        "cache_prefix_version": cache_prefix.get("prefix_version"),
                        "cache_resource_generation": (state.cache_binding or {}).get("resource_generation"),
                        "cache_content_fingerprint": cache_prefix.get("content_fingerprint") or cache_prefix.get("prefix_fingerprint"),
                        "cache_cost_observation": cache_cost_observation,
                    }
                )
                if cache_metadata.get("prefix_growth_decision") is not None:
                    cache_usage["prefix_growth_decision"] = cache_metadata.get("prefix_growth_decision")
            if state.upgrade and state.wire.get("normalize"):
                cache_usage["affinity_audit"] = {
                    "affinity_epoch": state.wire.get("affinity_epoch"),
                    "route": state.wire.get("route_observation") or {},
                    "physical_chain": state.wire.get("physical_chain") or [],
                    "semantic_fingerprint": state.wire.get("semantic_fingerprint"),
                    "prefix_diff": state.wire.get("prefix_diff"),
                    "compared_request_id": state.wire.get("compared_request_id"),
                    "read_tokens": cache_usage.get("cache_read_tokens"),
                    "implicit_active": bool((cache_metadata.get("tracks") or {}).get(
                        "implicit_effective" if int(state.upgrade.get("version") or 0) >= 2 else "implicit_base"
                    )),
                    "payload_sha256": state.wire.get("payload_sha256"),
                }
                cache_usage["cache_tracks"] = cache_metadata.get("tracks")
            if int(state.upgrade.get("version") or 0) >= 2:
                audit = normalize_observed_cache_audit(
                    protocol=str(state.snapshot.get("protocol") or ""), response_json=result.raw_json,
                    response_headers=state.wire.get("route_observation") or {},
                    track_facts=cache_metadata.get("tracks") or {},
                    payload_stripped_bytes=int((result.observed or {}).get("payload_stripped_bytes") or 0),
                )
                action = (
                    "BYPASS_OFF" if state.snapshot.get("requested_cache_mode") == "off"
                    else "IMPLICIT_WATCHDOG_BELOW_THRESHOLD" if cache_decision_reason == "insufficient_prefix_length"
                    else "ROLL_STATEFUL_SNAPSHOT" if cache_resource_action == "created" and cache_metadata.get("prefix_growth_decision")
                    else "CREATE_INITIAL_STATEFUL" if cache_resource_action == "created"
                    else "REUSE_STATEFUL_WITH_SUFFIX" if cache_mechanism == "stateful_resource"
                    else "BREAKPOINT_AND_IMPLICIT" if cache_mechanism == "breakpoint"
                    else "IMPLICIT_ONLY_ACTIVE" if cache_mechanism == "implicit_prefix"
                    else "UNCACHED_SAME_CONTEXT"
                )
                cache_usage.update(
                    observed_cache_audit=audit, payload_stripped_bytes=audit["payload_stripped_bytes"],
                    cache_decision_reason=cache_decision_reason, action=action,
                )
            cache_summary = {
                "material_reuse": state.material_reuse_observation if str(state.snapshot.get("provider") or "").lower() == "gemini" else None,
                "material_fingerprint": ((state.context_plan or {}).get("material_reuse") or {}).get("ordered_material_fingerprint"),
                "requested_cache_mode": state.snapshot.get("requested_cache_mode"),
                "requested_cache_mechanism": state.snapshot.get("requested_cache_mechanism", "auto"),
                "estimated_future_reuses": (plan.get("reuse_prediction") or {}).get("expected_future_reuses"),
                "reuse_evidence_source": (plan.get("reuse_prediction") or {}).get("source"),
                "selection_evidence": plan.get("selection_evidence") or {},
                "cache_cost_decision": cache_metadata.get("cache_cost_decision"),
                "planned_mechanism": plan.get("planned_mechanism"),
                "effective_cache_mechanism": cache_mechanism,
                "cache_tracks": cache_metadata.get("tracks"),
                "prefix_diff": state.wire.get("prefix_diff"),
                "execution_mechanism": cache_mechanism,
                # At this point the physical CacheExecutionBinding has been
                # installed and sealed, so any final-threshold guard is resolved.
                "resolution_status": "finalized",
                "decision_reason": cache_decision_reason,
                **{k: v for k, v in cache_usage.items() if k != "affinity_audit"},
            }

        await ensure_execution_allowed(states=("dispatch_started",))
        result = replace(result, normalized_usage={**result.normalized_usage, **(cache_usage or {})})
        observation = {"cache_summary": cache_summary, "cache_usage": cache_usage,
                       "normalize": bool(state.wire.get("normalize")), "recorded_at": utcnow().isoformat()}
        # Archive the standardized response and observations for execution-plane
        # recovery. Raw response bytes stay in their original object.
        archived_result = {name: getattr(result, name) for name in (
            "raw_json", "text", "response_id", "usage", "cached_tokens", "response_output", "history_entry",
            "http_status", "provider_request_id", "observed", "normalized_usage")}
        fact_object_id = await self.artifacts.store_object(state.request_row, "execution-fact.json", json_bytes({
            "schema_version": "relay-execution-fact/1", "request_id": state.request_id,
            "snapshot_hash": stable_hash(state.snapshot), "raw_sha256": __import__("hashlib").sha256(result.raw_bytes).hexdigest(),
            "result": archived_result, "observation": observation,
        }), "application/json")
        marker = {"_relay_execution_fact": fact_object_id, "schema_version": "relay-result-pending-commit/1"}
        if state.fence is not None:
            stored = await self.repo.store_result_v3(
                request_id=state.request_id, result_object_id=raw_object_id, output_object_id=output_object_id,
                history_object_id=None, compact_result=marker, provider_response_id=result.response_id,
                cache_usage=cache_usage, fence_owner=state.fence.owner, fence_epoch=state.fence.epoch)
            if not stored: raise ExecutionConflictError("A stale execution fence cannot publish result_stored")
        else:
            await self.repo.update_request(state.request_id, {"provider_dispatch_state": "result_stored",
                "provisional_result_object_id": raw_object_id, "provisional_output_object_id": output_object_id,
                "provisional_history_object_id": None, "provisional_compact_result": marker,
                "provider_response_id": result.response_id})
        return ActualExecutionFact(state.request_id, "result_stored", state.material_bindings, state.cache_binding,
            {"seal_id": state.request_id, "seal_hash": state.sealed_bundle.seal_hash}, result,
            raw_object_id, output_object_id, execution_observation=observation)
