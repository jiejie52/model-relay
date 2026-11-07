from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from typing import Any

from ..core.idempotency import stable_hash
from .breakpoints import prepare_breakpoint_binding
from .contracts import CacheDecisionError, CacheExecutionBinding, ExecutionFence, provider_managed_trial_enabled
from .prefix import prepare_implicit_prefix_binding
from .stateful import StatefulResourceManager
from .watchdog import assess_implicit_prefix
from .planner import UNIFIED_PLAN_VERSION, candidate_execution_plan, arbitrate_unified_plan, final_track_facts


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
        plan_hash = str(snapshot.get("cache_plan_hash") or "")
        upgrade = snapshot.get("cache_upgrade") or {}
        if (int(upgrade.get("version") or 0) > 4 or
                int(upgrade.get("version") or 0) == 4 and upgrade.get("grok_mode") != "legacy_v1"
                and plan.get("schema_version") != UNIFIED_PLAN_VERSION):
            raise CacheDecisionError("CACHE_PLAN_VERSION_UNSUPPORTED", "Request cache plan requires an unsupported runtime format")
        if plan.get("schema_version") == UNIFIED_PLAN_VERSION:
            return await self._prepare_unified(
                request_row=request_row, snapshot=snapshot, session=session, context_plan=context_plan,
                material_bindings=material_bindings, fence=fence, history=history,
                provider_physical_plan=provider_physical_plan,
            )
        mechanism = plan.get("planned_mechanism")
        material_binding_hash = stable_hash(material_bindings)
        if mechanism is None:
            return CacheExecutionBinding(
                mechanism=None,
                decision_reason=str(plan.get("decision_reason") or "legacy_unmanaged"),
                context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                plan_hash=plan_hash,
                material_binding_hash=material_binding_hash,
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
                history=history,
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
                cost_reason = str(cost_decision.get("reason") or prepared.get("decision_reason") or "")
                fallback = next(
                    (
                        item for item in plan.get("fallback_mechanisms", [])
                        if isinstance(item, dict) and item.get("mechanism") == "implicit_prefix"
                    ),
                    None,
                )
                independent = (plan.get("tracks") or {}).get("implicit") or {}
                new_tracks = int((snapshot.get("cache_upgrade") or {}).get("version") or 0) >= 3
                independent_plan = independent.get("plan") if new_tracks else None
                if independent.get("fallback_authorized") and isinstance(independent_plan, dict):
                    fallback = {
                        "mechanism": "implicit_prefix", "mechanism_config": independent_plan["mechanism_config"],
                        "resolution_status": independent_plan["resolution_status"],
                        "final_threshold_guard": independent_plan["final_threshold_guard"],
                    }
                economic_reasons = {
                    "explicit_not_economical_vs_implicit",
                    "current_read_and_future_bonus_insufficient",
                    "implicit_hit_rate_unverified",
                    "current_read_price_or_creation_fee_unverified",
                    "new_resource_cost_measurement_unavailable",
                    "measurement_unavailable",
                    "new_resource_budget_exceeded",
                    "hard_budget_price_or_storage_evidence_unverified",
                    "relative_price_policy_invalid",
                    "resource_measurement_invalid",
                }
                if new_tracks:
                    economic_reasons.add("no_cacheable_prefix")
                if (
                    str(plan.get("resolver_version") or "") in {"relay-cache-resolver/6", "relay-cache-resolver/7"}
                    and (plan.get("requested_mode") == "auto" or (
                        new_tracks and independent.get("fallback_authorized")
                        and cost_reason == "no_cacheable_prefix"
                    ))
                    and fallback is not None
                    and cost_reason in economic_reasons
                ):
                    fallback_plan = dict(plan)
                    fallback_plan["planned_mechanism"] = "implicit_prefix"
                    fallback_plan["mechanism_config"] = dict(fallback.get("mechanism_config") or {})
                    fallback_plan["resolution_status"] = str(fallback.get("resolution_status") or "finalized")
                    implicit_binding = await self._prepare_implicit_binding(
                        plan=fallback_plan,
                        plan_hash=plan_hash,
                        snapshot=snapshot,
                        session=session,
                        context_plan=context_plan,
                        material_binding_hash=material_binding_hash,
                        provider_physical_plan=provider_physical_plan,
                        history=history,
                    )
                    fallback_metadata = dict(implicit_binding.metadata or {})
                    fallback_metadata.update({
                        "cache_cost_decision": cost_decision,
                        "fallback_from": "stateful_resource",
                        "fallback_reason": cost_reason,
                    })
                    if implicit_binding.mechanism == "implicit_prefix":
                        return CacheExecutionBinding(
                            mechanism="implicit_prefix",
                            decision_reason="explicit_not_economical_fallback_to_implicit_prefix",
                            context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                            plan_hash=plan_hash,
                            prefix=dict(implicit_binding.prefix or {}),
                            material_binding_hash=material_binding_hash,
                            metadata=fallback_metadata,
                        )
                    return CacheExecutionBinding(
                        mechanism=None,
                        decision_reason="explicit_not_economical_implicit_fallback_unavailable",
                        context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                        plan_hash=plan_hash,
                        prefix=dict(implicit_binding.prefix or {}),
                        material_binding_hash=material_binding_hash,
                        metadata=fallback_metadata,
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
            )

        raise RuntimeError(f"Unsupported cache mechanism in frozen plan: {mechanism!r}")

    async def _prepare_unified(
        self, *, request_row, snapshot, session, context_plan, material_bindings, fence, history,
        provider_physical_plan,
    ) -> CacheExecutionBinding:
        plan = snapshot["cache_plan"]
        plan_hash = str(snapshot.get("cache_plan_hash") or "")
        if (plan_hash != plan.get("plan_hash")
                or plan_hash != stable_hash({k: v for k, v in plan.items() if k != "plan_hash"})
                or plan.get("context_plan_hash") != context_plan.get("context_plan_hash")):
            raise CacheDecisionError("CACHE_PLAN_INTEGRITY_MISMATCH", "Unified cache plan does not match the accepted snapshot")
        candidates = plan.get("candidates") or {}
        material_hash = stable_hash(material_bindings)
        assessments = {}
        implicit_binding = None
        controlled_result = None
        controlled_plan = None
        controlled = (plan.get("tracks") or {}).get("controlled", {}).get("candidate")
        safe_reasons = (plan.get("arbitration") or {}).get("allowed_fallback_reasons") or []
        assessment_failures = (plan.get("arbitration") or {}).get("assessment_failure_reasons") or []
        common = {"request_row": request_row, "snapshot": snapshot, "session": session,
                  "context_plan": context_plan, "material_bindings": material_bindings,
                  "fence": fence, "history": history, "provider_physical_plan": provider_physical_plan}

        # These are read-only Provider assessments. No track creates a cache
        # resource before centralized arbitration has selected its branch.
        if "implicit_prefix" in candidates:
            implicit_plan = candidate_execution_plan(plan, "implicit_prefix")
            try:
                implicit_binding = await self._prepare_implicit_binding(
                    plan=implicit_plan, plan_hash=plan_hash, snapshot=snapshot, session=session,
                    context_plan=context_plan, material_binding_hash=material_hash,
                    provider_physical_plan=provider_physical_plan, history=history,
                )
            except CacheDecisionError as exc:
                if exc.reason not in assessment_failures:
                    raise
                implicit_binding = CacheExecutionBinding(
                    mechanism=None, decision_reason=exc.reason, context_plan_hash=context_plan["context_plan_hash"],
                    plan_hash=plan_hash, material_binding_hash=material_hash, metadata={"assessment_error": exc.code},
                )
            assessments["implicit_prefix"] = {
                "eligible": implicit_binding.mechanism == "implicit_prefix",
                "reason": implicit_binding.decision_reason,
                "threshold": dict(implicit_binding.metadata),
            }
        if controlled == "stateful_resource":
            controlled_plan = deepcopy(candidate_execution_plan(plan, controlled))
            if not assessments.get("implicit_prefix", {}).get("eligible"):
                economics = controlled_plan["mechanism_config"].get("auto_economics") or {}
                economics.update(certified_implicit_alternative=False, trial_implicit_alternative=False)
            try:
                controlled_result = await self.stateful.prepare(plan=controlled_plan, assessment_only=True, **common)
            except CacheDecisionError as exc:
                if exc.reason not in assessment_failures:
                    raise
                controlled_result = {"mechanism": None, "decision_reason": exc.reason,
                                     "metadata": {"assessment_error": exc.code}}
            reason = str(controlled_result.get("decision_reason") or "unknown")
            if controlled_result.get("mechanism") is None and reason not in safe_reasons and reason not in assessment_failures:
                raise CacheDecisionError("CACHE_ASSESSMENT_FAILED", "Resource assessment did not establish a safe branch", reason=reason)
            meta = controlled_result.get("metadata") or {}
            assessments[controlled] = {"eligible": controlled_result.get("mechanism") == controlled,
                                       "reason": reason, "resource_action": (meta.get("cache_resource_action")
                                           or ("reuse" if reason.startswith("resource_reused") else None)),
                                       "cost": meta.get("cache_cost_decision") or {},
                                       "prefix_token_count": meta.get("prefix_token_count")}
        elif controlled == "breakpoint":
            controlled_plan = candidate_execution_plan(plan, controlled)
            anchors = prepare_breakpoint_binding(plan=controlled_plan, context_plan=context_plan)
            controlled_result = CacheExecutionBinding(
                mechanism=controlled if anchors else None,
                decision_reason="selected" if anchors else "no_cacheable_prefix",
                context_plan_hash=context_plan["context_plan_hash"], plan_hash=plan_hash,
                material_binding_hash=material_hash, breakpoint_anchors=anchors,
            )
            assessments[controlled] = {"eligible": bool(anchors), "reason": controlled_result.decision_reason}

        decision = arbitrate_unified_plan(plan, assessments)
        selected = decision["selected_mechanisms"]
        if "stateful_resource" in selected:
            try:
                prepared = await self.stateful.prepare(plan=controlled_plan, **common)
            except CacheDecisionError as exc:
                if (exc.reason not in safe_reasons or plan.get("requested_mode") == "on"
                        and plan.get("requested_mechanism") != "auto" and exc.reason != "below_minimum"):
                    raise
                prepared = {"mechanism": None, "decision_reason": exc.reason}
            if prepared.get("mechanism") is None:
                reason = str(prepared.get("decision_reason") or "unknown")
                if reason not in safe_reasons:
                    # In particular, auth, transport and create_unknown facts
                    # never authorize a different cache path or another create.
                    raise CacheDecisionError("CACHE_MATERIALIZATION_FAILED", "Selected cache branch did not materialize safely", reason=reason)
                assessments["stateful_resource"] = {"eligible": False, "reason": reason}
                decision = arbitrate_unified_plan(plan, assessments)
                selected = decision["selected_mechanisms"]
            else:
                binding = CacheExecutionBinding(
                    mechanism="stateful_resource", decision_reason=str(prepared.get("decision_reason") or "selected"),
                    context_plan_hash=context_plan["context_plan_hash"], plan_hash=plan_hash,
                    resource_id=prepared.get("resource_id"), resource_generation=prepared.get("resource_generation"),
                    provider_handle=prepared.get("provider_handle"), expires_at=prepared.get("expires_at"),
                    material_binding_hash=material_hash, prefix=dict(prepared.get("prefix") or {}),
                    projection_version=str(prepared.get("projection_version") or "relay-cache-projection/1"),
                    metadata=dict(prepared.get("metadata") or {}),
                )
        if "stateful_resource" not in selected:
            if "breakpoint" in selected:
                binding = controlled_result
            elif "implicit_prefix" in selected:
                binding = implicit_binding
            else:
                binding = CacheExecutionBinding(
                    mechanism=None, decision_reason=plan.get("decision_reason") if not candidates else decision["reason"],
                    context_plan_hash=context_plan["context_plan_hash"], plan_hash=plan_hash,
                    material_binding_hash=material_hash,
                )
        tracks = final_track_facts(plan, binding.mechanism)
        tracks["implicit_effective"] = "implicit_prefix" in selected
        return replace(binding, metadata={**binding.metadata, "arbitration": decision, "tracks": tracks})

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
        history: list[dict[str, Any]] | None = None,
    ) -> CacheExecutionBinding:
        cfg = plan.get("mechanism_config") or {}
        watchdog_metadata = {}
        if cfg.get("implicit_watchdog"):
            try:
                tokens, source = await assess_implicit_prefix(
                    self.stateful.registry, snapshot, context_plan, history or [],
                )
            except CacheDecisionError:
                prepared = self.stateful._prepare_failure(
                    plan, reason="prefix_assessment_unknown", message="Stable prefix eligibility cannot be measured",
                )
                return CacheExecutionBinding(
                    mechanism=prepared.get("mechanism"), decision_reason=prepared["decision_reason"],
                    context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                    plan_hash=plan_hash, material_binding_hash=material_binding_hash,
                )
            minimum = int(cfg["minimum_cacheable_tokens"])
            watchdog_metadata = {
                "prefix_token_count": tokens, "minimum_cacheable_tokens": minimum,
                "threshold_measurement": source,
            }
            if tokens < minimum:
                return CacheExecutionBinding(
                    mechanism=None, decision_reason="insufficient_prefix_length",
                    context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                    plan_hash=plan_hash, material_binding_hash=material_binding_hash,
                    metadata=watchdog_metadata,
                )
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
            )

        threshold_metadata: dict[str, Any] = dict(watchdog_metadata)
        cfg = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
        trial = cfg.get("threshold_mode") == "provider_managed_trial"
        if trial:
            contract = snapshot.get("cache_contract") or {}
            profile = snapshot.get("protocol_profile") or {}
            supply_cfg = (contract.get("mechanism_profiles") or {}).get("implicit_prefix") or {}
            wire_cache = profile.get("cache") or {}
            wire_cfg = (wire_cache.get("mechanism_profiles") or {}).get("implicit_prefix") or {}
            if not (
                str(plan.get("resolver_version") or "") == "relay-cache-resolver/7"
                and plan.get("requested_mode") == "auto"
                and str(snapshot.get("provider") or "").lower() == "gemini"
                and profile.get("protocol") == "gemini_native"
                and "implicit_prefix" in (contract.get("supported_mechanisms") or [])
                and "implicit_prefix" in (wire_cache.get("supported_mechanisms") or [])
                and cfg.get("admission_status") == "provider_managed_trial"
                and all(provider_managed_trial_enabled(c) for c in (cfg, supply_cfg, wire_cfg))
            ):
                raise CacheDecisionError("CACHE_TRIAL_NOT_AUTHORIZED", "Frozen implicit-prefix trial grant is missing or invalid")
            threshold_metadata = {
                "admission_status": "provider_managed_trial",
                "threshold_measurement": "provider_managed_trial",
                "provider_hit_guaranteed": False,
            }
        if (not trial and str(snapshot.get("provider") or "").lower() == "gemini"
                and snapshot.get("protocol", "gemini_native") == "gemini_native"
                and str(plan.get("resolution_status") or "") == "pending"):
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
                )

            physical = provider_physical_plan if isinstance(provider_physical_plan, dict) else {}
            measurement_payload = physical.get("implicit_prefix_measurement_payload")
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
                )
            try:
                measured_tokens = int(
                    await adapter.measure(
                        spec={
                            "model": str(snapshot.get("model") or ""),
                            "provider_payload": measurement_payload,
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
                )
            threshold_metadata = {
                "prefix_token_count": measured_tokens,
                "threshold_measurement": "gemini_countTokens",
                "minimum_cacheable_tokens": minimum,
            }
            if measured_tokens < minimum:
                if (plan.get("requested_mode") == "on"
                    and plan.get("requested_mechanism") == "implicit_prefix"
                    and not snapshot.get("cache_upgrade")):
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
                )
        return CacheExecutionBinding(
            mechanism="implicit_prefix",
            decision_reason="selected_provider_managed_trial" if trial else "selected",
            context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
            plan_hash=plan_hash,
            prefix={**prefix, **threshold_metadata},
            material_binding_hash=material_binding_hash,
            metadata=threshold_metadata,
        )
