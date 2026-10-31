from __future__ import annotations

from typing import Any

from ..core.idempotency import stable_hash
from .breakpoints import prepare_breakpoint_binding
from .contracts import CacheDecisionError, CacheExecutionBinding, ExecutionFence
from .prefix import prepare_implicit_prefix_binding
from .stateful import StatefulResourceManager


class CacheOrchestrator:
    def __init__(self, repo: Any, *, resource_registry: Any = None) -> None:
        self.repo = repo
        self.stateful = StatefulResourceManager(repo, resource_registry)

    async def prepare(
        self,
        *,
        request_row: dict[str, Any],
        snapshot: dict[str, Any],
        session: dict[str, Any],
        context_plan: dict[str, Any],
        material_bindings: list[dict[str, Any]],
        fence: ExecutionFence,
        history: list[dict[str, Any]],
        provider_physical_plan: dict[str, Any] | None = None,
    ) -> CacheExecutionBinding:
        plan = snapshot.get("cache_plan") if isinstance(snapshot.get("cache_plan"), dict) else {}
        if not isinstance(plan.get("primary_binding"), dict) and not isinstance(plan.get("additional_cache_bindings"), list):
            return await self._prepare_single(
                request_row=request_row,
                snapshot=snapshot,
                session=session,
                context_plan=context_plan,
                material_bindings=material_bindings,
                fence=fence,
                history=history,
                provider_physical_plan=provider_physical_plan,
            )

        plan_hash = str(snapshot.get("cache_plan_hash") or "")
        material_binding_hash = stable_hash(material_bindings)
        primary_plan = plan.get("primary_binding") if isinstance(plan.get("primary_binding"), dict) else None
        if primary_plan is None:
            primary_plan = {
                "binding_id": "primary",
                "role": "primary",
                "mechanism": plan.get("planned_mechanism"),
                "projection": plan.get("primary_projection") or {"kind": "full_stable_prefix"},
                "mechanism_config": plan.get("mechanism_config") or {},
                "resolution_status": plan.get("resolution_status") or "finalized",
                "required": plan.get("requested_mode") == "on",
                "decision_reason": plan.get("decision_reason") or "selected",
            }

        def single_plan(source: dict[str, Any], binding_plan: dict[str, Any], *, keep_fallbacks: bool) -> dict[str, Any]:
            value = dict(source)
            value["planned_mechanism"] = binding_plan.get("mechanism")
            value["mechanism_config"] = dict(binding_plan.get("mechanism_config") or {})
            value["primary_projection"] = dict(binding_plan.get("projection") or {"kind": "full_stable_prefix"})
            value["binding_plan"] = {
                "binding_id": str(binding_plan.get("binding_id") or "primary"),
                "role": str(binding_plan.get("role") or "primary"),
                "mechanism": binding_plan.get("mechanism"),
                "projection": value["primary_projection"],
                "projection_hash": binding_plan.get("projection_hash") or value["primary_projection"].get("projection_hash"),
                "required": binding_plan.get("required") is True,
            }
            value["fallback_mechanisms"] = list(source.get("fallback_mechanisms") or []) if keep_fallbacks else []
            value["additional_cache_bindings"] = []
            value.pop("primary_binding", None)
            value.pop("binding_plan_version", None)
            value.pop("binding_composition", None)
            value["decision_reason"] = str(binding_plan.get("decision_reason") or source.get("decision_reason") or "selected")
            value["resolution_status"] = str(binding_plan.get("resolution_status") or source.get("resolution_status") or "finalized")
            if str(binding_plan.get("role") or "primary") == "additional":
                value["requested_mode"] = "on" if binding_plan.get("required") is True else "auto"
                value["requested_mechanism"] = str(binding_plan.get("mechanism") or "auto") if binding_plan.get("required") is True else "auto"
            return value

        primary_snapshot = dict(snapshot)
        primary_snapshot["cache_plan"] = single_plan(plan, primary_plan, keep_fallbacks=True)
        primary_result = await self._prepare_single(
            request_row=request_row,
            snapshot=primary_snapshot,
            session=session,
            context_plan=context_plan,
            material_bindings=material_bindings,
            fence=fence,
            history=history,
            provider_physical_plan=provider_physical_plan,
        )
        primary_projection = dict(
            primary_result.projection
            or primary_plan.get("projection")
            or {"kind": "full_stable_prefix"}
        )
        primary_record = primary_result.canonical(include_provider_handle=True)["primary_binding"]
        primary_mechanism_config = dict(primary_result.mechanism_config or primary_plan.get("mechanism_config") or {})
        primary_record.update({
            "binding_id": "primary",
            "role": "primary",
            "projection": primary_projection,
            "projection_hash": primary_projection.get("projection_hash"),
            "mechanism_config": primary_mechanism_config,
            "required": primary_plan.get("required") is True,
            "state": (
                "attached" if primary_result.mechanism is not None
                else "unknown" if str(primary_result.decision_reason).endswith("unknown")
                else "skipped"
            ),
        })

        additional_records: list[dict[str, Any]] = []
        for binding_plan in plan.get("additional_cache_bindings") or []:
            if not isinstance(binding_plan, dict):
                continue
            required = binding_plan.get("required") is True
            binding_snapshot = dict(snapshot)
            binding_snapshot["cache_plan"] = single_plan(plan, binding_plan, keep_fallbacks=False)
            try:
                prepared = await self._prepare_single(
                    request_row=request_row,
                    snapshot=binding_snapshot,
                    session=session,
                    context_plan=context_plan,
                    material_bindings=material_bindings,
                    fence=fence,
                    history=history,
                    provider_physical_plan=provider_physical_plan,
                )
            except CacheDecisionError as exc:
                if required:
                    raise
                additional_records.append({
                    "binding_id": str(binding_plan.get("binding_id") or "additional"),
                    "role": "additional",
                    "mechanism": binding_plan.get("mechanism"),
                    "active": False,
                    "state": "unknown" if exc.reason in {"cache_create_unknown", "cache_operation_fence_rejected"} else "failed",
                    "resource_action": "unknown" if exc.reason in {"cache_create_unknown", "cache_operation_fence_rejected"} else "failed",
                    "decision_reason": str(exc.reason or exc.code),
                    "required": False,
                    "projection": dict(binding_plan.get("projection") or {}),
                    "projection_hash": binding_plan.get("projection_hash"),
                    "resource_id": None,
                    "resource_generation": None,
                    "provider_handle": None,
                    "metadata": {"error_code": exc.code},
                })
                continue

            record = prepared.canonical(include_provider_handle=True)["primary_binding"]
            projection = dict(binding_plan.get("projection") or {"kind": "full_stable_prefix"})
            record.update({
                "binding_id": str(binding_plan.get("binding_id") or "additional"),
                "role": "additional",
                "projection": projection,
                "projection_hash": binding_plan.get("projection_hash") or projection.get("projection_hash"),
                "mechanism_config": dict(binding_plan.get("mechanism_config") or {}),
                "required": required,
                "selection_evidence": dict(binding_plan.get("selection_evidence") or {}),
                "state": "attached" if prepared.mechanism is not None else "skipped",
                "resource_action": record.get("resource_action") or (
                    "provider_managed" if prepared.mechanism == "implicit_prefix" else "skipped"
                ),
            })
            if required and prepared.mechanism is None:
                raise CacheDecisionError(
                    "CACHE_ADDITIONAL_BINDING_REQUIRED",
                    "A required additional cache binding could not be prepared",
                    reason=str(prepared.decision_reason or "additional_binding_not_prepared"),
                )
            additional_records.append(record)

        primary_metadata = dict(primary_result.metadata or {})
        primary_metadata["cache_binding_set"] = {
            "version": "relay-cache-binding-set/2",
            "primary_binding_id": "primary",
            "planned_additional_binding_count": len(plan.get("additional_cache_bindings") or []),
            "prepared_additional_binding_count": sum(1 for item in additional_records if item.get("active")),
            "binding_composition": dict(plan.get("binding_composition") or {}),
        }
        return CacheExecutionBinding(
            mechanism=primary_result.mechanism,
            decision_reason=primary_result.decision_reason,
            context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
            plan_hash=plan_hash,
            binding_version=primary_result.binding_version,
            resource_id=primary_result.resource_id,
            resource_generation=primary_result.resource_generation,
            provider_handle=primary_result.provider_handle,
            expires_at=primary_result.expires_at,
            breakpoint_anchors=primary_result.breakpoint_anchors,
            prefix=dict(primary_result.prefix or {}),
            material_binding_hash=material_binding_hash,
            projection_version=primary_result.projection_version,
            metadata=primary_metadata,
            binding_id="primary",
            role="primary",
            projection=primary_projection,
            projection_hash=primary_projection.get("projection_hash"),
            mechanism_config=primary_mechanism_config,
            additional_bindings=tuple(additional_records),
        )

    async def _prepare_single(
        self,
        *,
        request_row: dict[str, Any],
        snapshot: dict[str, Any],
        session: dict[str, Any],
        context_plan: dict[str, Any],
        material_bindings: list[dict[str, Any]],
        fence: ExecutionFence,
        history: list[dict[str, Any]],
        provider_physical_plan: dict[str, Any] | None = None,
    ) -> CacheExecutionBinding:
        plan = snapshot.get("cache_plan") if isinstance(snapshot.get("cache_plan"), dict) else {}
        plan_hash = str(snapshot.get("cache_plan_hash") or "")
        mechanism = plan.get("planned_mechanism")
        material_binding_hash = stable_hash(material_bindings)
        if mechanism is None:
            return CacheExecutionBinding(
                mechanism=None,
                decision_reason=str(plan.get("decision_reason") or "legacy_unmanaged"),
                context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                plan_hash=plan_hash,
                material_binding_hash=material_binding_hash,
                projection=dict(plan.get("primary_projection") or {"kind": "full_stable_prefix"}),
                projection_hash=(plan.get("primary_projection") or {}).get("projection_hash"),
                mechanism_config=dict(plan.get("mechanism_config") or {}),
            )

        if mechanism == "implicit_prefix":
            return await self._prepare_implicit_binding(
                plan=plan,
                plan_hash=plan_hash,
                snapshot=snapshot,
                session=session,
                context_plan=context_plan,
                material_binding_hash=material_binding_hash,
                provider_physical_plan=provider_physical_plan,
            )

        if mechanism == "breakpoint":
            anchors = prepare_breakpoint_binding(plan=plan, context_plan=context_plan)
            if not anchors:
                return CacheExecutionBinding(
                    mechanism=None,
                    decision_reason="no_cacheable_prefix",
                    context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                    plan_hash=plan_hash,
                    material_binding_hash=material_binding_hash,
                )
            return CacheExecutionBinding(
                mechanism="breakpoint",
                decision_reason="selected",
                context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                plan_hash=plan_hash,
                breakpoint_anchors=anchors,
                material_binding_hash=material_binding_hash,
                projection=dict(plan.get("primary_projection") or {"kind": "full_stable_prefix"}),
                projection_hash=(plan.get("primary_projection") or {}).get("projection_hash"),
                mechanism_config=dict(plan.get("mechanism_config") or {}),
            )

        if mechanism == "stateful_resource":
            prepared = await self.stateful.prepare(
                request_row=request_row,
                plan=plan,
                context_plan=context_plan,
                material_bindings=material_bindings,
                fence=fence,
                snapshot=snapshot,
                session=session,
                history=history,
                provider_physical_plan=provider_physical_plan,
            )
            if prepared.get("mechanism") is None:
                metadata = dict(prepared.get("metadata") or {})
                cost_decision = metadata.get("cache_cost_decision") if isinstance(metadata.get("cache_cost_decision"), dict) else {}
                prepare_reason = str(prepared.get("decision_reason") or "")
                cacheability_reason = str(metadata.get("cacheability_reason") or "")
                cost_reason = str(cost_decision.get("reason") or prepare_reason or cacheability_reason or "")
                fallback = next(
                    (
                        item for item in plan.get("fallback_mechanisms", [])
                        if isinstance(item, dict) and item.get("mechanism") == "implicit_prefix"
                    ),
                    None,
                )
                economic_reasons = {
                    "explicit_not_economical_vs_implicit",
                    "current_read_and_future_bonus_insufficient",
                    "implicit_hit_rate_unverified",
                    "current_read_price_or_creation_fee_unverified",
                    "new_resource_cost_measurement_unavailable",
                    "measurement_unavailable",
                    "new_resource_budget_exceeded",
                    "hard_budget_price_or_storage_evidence_unverified",
                    "hard_budget_cap_invalid",
                    "hard_budget_creation_ratio_unverified",
                    "relative_price_policy_invalid",
                    "resource_measurement_invalid",
                }
                safe_explicit_failures_v7 = {
                    "cache_create_rejected",
                    "cache_resource_probe_failed",
                    "no_cacheable_prefix",
                    "stateful_adapter_unavailable",
                    "cache_measurement_failed",
                    "measurement_unavailable",
                    "below_minimum",
                }
                safe_projection_failures_v7 = {
                    "serialized_request_too_large",
                    "request_material_content_boundary_not_equivalent",
                    "no_cacheable_contents",
                    "history_material_projection_unsafe",
                    "cached_content_unsupported_provider_fields",
                    "provider_projection_not_supported",
                    "physical_binding_projection_unavailable",
                }
                resolver_version = str(plan.get("resolver_version") or "")
                resolver6_fallback = (
                    resolver_version == "relay-cache-resolver/6"
                    and cost_reason in economic_reasons
                )
                resolver7_fallback = (
                    resolver_version in {"relay-cache-resolver/7", "relay-cache-resolver/8"}
                    and (
                        prepare_reason in economic_reasons
                        or str(cost_decision.get("reason") or "") in economic_reasons
                        or prepare_reason in safe_explicit_failures_v7
                        or prepare_reason in safe_projection_failures_v7
                        or cacheability_reason in safe_projection_failures_v7
                    )
                )
                if (
                    (resolver6_fallback or resolver7_fallback)
                    and plan.get("requested_mode") == "auto"
                    and fallback is not None
                ):
                    fallback_plan = dict(plan)
                    fallback_plan["planned_mechanism"] = "implicit_prefix"
                    fallback_plan["mechanism_config"] = dict(fallback.get("mechanism_config") or {})
                    fallback_plan["resolution_status"] = str(fallback.get("resolution_status") or "finalized")
                    fallback_projection = dict(fallback.get("projection") or plan.get("primary_projection") or {"kind": "full_stable_prefix"})
                    fallback_plan["primary_projection"] = fallback_projection
                    fallback_plan["binding_plan"] = {
                        **(dict(plan.get("binding_plan") or {})),
                        "mechanism": "implicit_prefix",
                        "projection": fallback_projection,
                        "projection_hash": fallback.get("projection_hash") or fallback_projection.get("projection_hash"),
                    }
                    implicit_binding = await self._prepare_implicit_binding(
                        plan=fallback_plan,
                        plan_hash=plan_hash,
                        snapshot=snapshot,
                        session=session,
                        context_plan=context_plan,
                        material_binding_hash=material_binding_hash,
                        provider_physical_plan=provider_physical_plan,
                    )
                    fallback_metadata = dict(metadata)
                    fallback_metadata.update(dict(implicit_binding.metadata or {}))
                    fallback_metadata.update({
                        "cache_cost_decision": cost_decision,
                        "fallback_from": "stateful_resource",
                        "fallback_reason": cost_reason,
                        "explicit_cache_action": (
                            "failed"
                            if prepare_reason in {
                                "cache_create_rejected",
                                "cache_resource_probe_failed",
                                "cache_measurement_failed",
                            }
                            else "skipped"
                        ),
                        "explicit_cache_skip_reason": cost_reason,
                        "implicit_prefix_capability": "certified",
                        "implicit_prefix_eligible": True,
                        "implicit_prefix_attempted": implicit_binding.mechanism == "implicit_prefix",
                    })
                    if implicit_binding.mechanism == "implicit_prefix":
                        explicit_failure = prepare_reason in {
                            "cache_create_rejected",
                            "cache_resource_probe_failed",
                            "cache_measurement_failed",
                        }
                        explicit_ineligible = (
                            prepare_reason == "no_cacheable_prefix"
                            or prepare_reason in safe_projection_failures_v7
                            or cacheability_reason in safe_projection_failures_v7
                            or prepare_reason in {
                                "stateful_adapter_unavailable",
                                "below_minimum",
                                "measurement_unavailable",
                            }
                        )
                        return CacheExecutionBinding(
                            mechanism="implicit_prefix",
                            decision_reason=(
                                "explicit_cache_failure_fallback_to_implicit_prefix" if explicit_failure
                                else "explicit_cache_ineligible_fallback_to_implicit_prefix" if explicit_ineligible
                                else "explicit_not_economical_fallback_to_implicit_prefix"
                            ),
                            context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                            plan_hash=plan_hash,
                            prefix=dict(implicit_binding.prefix or {}),
                            material_binding_hash=material_binding_hash,
                            metadata=fallback_metadata,
                            projection=fallback_projection,
                            projection_hash=fallback.get("projection_hash") or fallback_projection.get("projection_hash"),
                        )
                    return CacheExecutionBinding(
                        mechanism=None,
                        decision_reason="explicit_cache_fallback_implicit_unavailable",
                        context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                        plan_hash=plan_hash,
                        prefix=dict(implicit_binding.prefix or {}),
                        material_binding_hash=material_binding_hash,
                        metadata=fallback_metadata,
                        projection=fallback_projection,
                        projection_hash=fallback.get("projection_hash") or fallback_projection.get("projection_hash"),
                        mechanism_config=dict(fallback.get("mechanism_config") or {}),
                    )
            return CacheExecutionBinding(
                mechanism=prepared.get("mechanism"),
                decision_reason=str(prepared.get("decision_reason") or "selected"),
                context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                plan_hash=plan_hash,
                resource_id=prepared.get("resource_id"),
                resource_generation=prepared.get("resource_generation"),
                provider_handle=prepared.get("provider_handle"),
                expires_at=prepared.get("expires_at"),
                material_binding_hash=material_binding_hash,
                projection_version=str(prepared.get("projection_version") or "relay-cache-projection/1"),
                prefix=dict(prepared.get("prefix") or {}),
                metadata=dict(prepared.get("metadata") or {}),
                projection=dict(plan.get("primary_projection") or {"kind": "full_stable_prefix"}),
                projection_hash=(plan.get("primary_projection") or {}).get("projection_hash"),
                mechanism_config=dict(plan.get("mechanism_config") or {}),
            )

        raise RuntimeError(f"Unsupported cache mechanism in frozen plan: {mechanism!r}")

    async def _prepare_implicit_binding(
        self,
        *,
        plan: dict[str, Any],
        plan_hash: str,
        snapshot: dict[str, Any],
        session: dict[str, Any],
        context_plan: dict[str, Any],
        material_binding_hash: str,
        provider_physical_plan: dict[str, Any] | None,
    ) -> CacheExecutionBinding:
        try:
            prefix = prepare_implicit_prefix_binding(
                plan=plan,
                context_plan=context_plan,
                session=session,
                provider=str(snapshot.get("provider") or ""),
                snapshot=snapshot,
                provider_physical_plan=provider_physical_plan,
            )
        except CacheDecisionError as exc:
            prepared = self.stateful._prepare_failure(
                plan,
                reason=exc.reason or exc.code.lower(),
                message=exc.message,
            )
            return CacheExecutionBinding(
                mechanism=prepared.get("mechanism"),
                decision_reason=str(prepared.get("decision_reason") or exc.reason or exc.code.lower()),
                context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                plan_hash=plan_hash,
                material_binding_hash=material_binding_hash,
                metadata=dict(prepared.get("metadata") or {}),
                projection=dict(plan.get("primary_projection") or {"kind": "full_stable_prefix"}),
                projection_hash=(plan.get("primary_projection") or {}).get("projection_hash"),
                mechanism_config=dict(plan.get("mechanism_config") or {}),
            )

        threshold_metadata: dict[str, Any] = {}
        if str(snapshot.get("provider") or "").lower() == "gemini" and str(plan.get("resolution_status") or "") == "pending":
            cfg = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
            minimum = cfg.get("minimum_cacheable_tokens")
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
                prepared = self.stateful._prepare_failure(
                    plan,
                    reason="implicit_prefix_threshold_unverified",
                    message="Gemini implicit-prefix threshold guard has no valid frozen minimum token count",
                )
                return CacheExecutionBinding(
                    mechanism=prepared.get("mechanism"),
                    decision_reason=str(prepared.get("decision_reason") or "implicit_prefix_threshold_unverified"),
                    context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                    plan_hash=plan_hash,
                    material_binding_hash=material_binding_hash,
                    metadata=dict(prepared.get("metadata") or {}),
                    projection=dict(plan.get("primary_projection") or {"kind": "full_stable_prefix"}),
                    projection_hash=(plan.get("primary_projection") or {}).get("projection_hash"),
                    mechanism_config=dict(plan.get("mechanism_config") or {}),
                )

            physical = provider_physical_plan if isinstance(provider_physical_plan, dict) else {}
            binding_plan = plan.get("binding_plan") if isinstance(plan.get("binding_plan"), dict) else {}
            projection = plan.get("primary_projection") if isinstance(plan.get("primary_projection"), dict) else {}
            if str(projection.get("kind") or "full_stable_prefix") != "full_stable_prefix":
                projection_map = physical.get("cache_binding_projections") if isinstance(physical.get("cache_binding_projections"), dict) else {}
                physical = projection_map.get(str(binding_plan.get("binding_id") or "")) if isinstance(projection_map.get(str(binding_plan.get("binding_id") or "")), dict) else {}
            measurement_payload = physical.get("implicit_prefix_measurement_payload") if isinstance(physical, dict) else None
            adapter = (
                self.stateful.registry.maybe_get(str(snapshot.get("connection_id") or ""))
                if self.stateful.registry is not None
                else None
            )
            if adapter is None or not isinstance(measurement_payload, dict):
                prepared = self.stateful._prepare_failure(
                    plan,
                    reason="implicit_prefix_measurement_unavailable",
                    message="Gemini implicit-prefix threshold cannot be verified by the frozen provider adapter",
                )
                return CacheExecutionBinding(
                    mechanism=prepared.get("mechanism"),
                    decision_reason=str(prepared.get("decision_reason") or "implicit_prefix_measurement_unavailable"),
                    context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                    plan_hash=plan_hash,
                    material_binding_hash=material_binding_hash,
                    metadata=dict(prepared.get("metadata") or {}),
                    projection=dict(plan.get("primary_projection") or {"kind": "full_stable_prefix"}),
                    projection_hash=(plan.get("primary_projection") or {}).get("projection_hash"),
                    mechanism_config=dict(plan.get("mechanism_config") or {}),
                )
            try:
                measured_tokens = int(
                    await adapter.measure(
                        spec={
                            "model": str(snapshot.get("model") or ""),
                            "provider_payload": measurement_payload,
                            "cache_binding_id": str(binding_plan.get("binding_id") or "primary"),
                            "projection_hash": projection.get("projection_hash"),
                        }
                    )
                )
            except Exception as exc:
                prepared = self.stateful._provider_prepare_failure(
                    plan,
                    exc,
                    "implicit_prefix_measurement_failed",
                )
                return CacheExecutionBinding(
                    mechanism=prepared.get("mechanism"),
                    decision_reason=str(prepared.get("decision_reason") or "implicit_prefix_measurement_failed"),
                    context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                    plan_hash=plan_hash,
                    material_binding_hash=material_binding_hash,
                    metadata=dict(prepared.get("metadata") or {}),
                    projection=dict(plan.get("primary_projection") or {"kind": "full_stable_prefix"}),
                    projection_hash=(plan.get("primary_projection") or {}).get("projection_hash"),
                    mechanism_config=dict(plan.get("mechanism_config") or {}),
                )
            threshold_metadata = {
                "prefix_token_count": measured_tokens,
                "threshold_measurement": "gemini_countTokens",
                "minimum_cacheable_tokens": minimum,
                "binding_id": str(binding_plan.get("binding_id") or "primary"),
                "projection_hash": projection.get("projection_hash"),
            }
            if measured_tokens < minimum:
                if (plan.get("requested_mode") == "on"
                    and plan.get("requested_mechanism") == "implicit_prefix"):
                    raise CacheDecisionError(
                        "CACHE_THRESHOLD_NOT_MET",
                        "Forced implicit-prefix projection is below the certified minimum",
                        reason="below_minimum",
                    )
                return CacheExecutionBinding(
                    mechanism=None,
                    decision_reason="below_minimum",
                    context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                    plan_hash=plan_hash,
                    prefix={**prefix, **threshold_metadata},
                    material_binding_hash=material_binding_hash,
                    metadata=threshold_metadata,
                    projection=dict(plan.get("primary_projection") or {"kind": "full_stable_prefix"}),
                    projection_hash=(plan.get("primary_projection") or {}).get("projection_hash"),
                    mechanism_config=dict(plan.get("mechanism_config") or {}),
                )
        return CacheExecutionBinding(
            mechanism="implicit_prefix",
            decision_reason="selected",
            context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
            plan_hash=plan_hash,
            prefix={**prefix, **threshold_metadata},
            material_binding_hash=material_binding_hash,
            metadata=threshold_metadata,
            projection=dict(plan.get("primary_projection") or {"kind": "full_stable_prefix"}),
            projection_hash=(plan.get("primary_projection") or {}).get("projection_hash"),
        )
