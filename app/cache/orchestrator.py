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
            )

        if mechanism == "implicit_prefix":
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
                    )
            return CacheExecutionBinding(
                mechanism="implicit_prefix",
                decision_reason="selected",
                context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                plan_hash=plan_hash,
                prefix={**prefix, **threshold_metadata},
                material_binding_hash=material_binding_hash,
                metadata=threshold_metadata,
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
