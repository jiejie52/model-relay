from __future__ import annotations

from typing import Any

from ..core.idempotency import stable_hash
from .breakpoints import prepare_breakpoint_binding
from .contracts import CacheExecutionBinding, ExecutionFence
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
            prefix = prepare_implicit_prefix_binding(
                plan=plan, context_plan=context_plan, session=session
            )
            return CacheExecutionBinding(
                mechanism="implicit_prefix",
                decision_reason="selected",
                context_plan_hash=str(context_plan.get("context_plan_hash") or ""),
                plan_hash=plan_hash,
                prefix=prefix,
                material_binding_hash=material_binding_hash,
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
            )

        raise RuntimeError(f"Unsupported cache mechanism in frozen plan: {mechanism!r}")
