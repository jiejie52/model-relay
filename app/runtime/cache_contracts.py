from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from app.core.idempotency import stable_hash
from app.control_plane.cache_contracts import CacheMode, CacheMechanism, CacheResolutionStatus, ExplicitCacheMaterialMode, explicit_cache_material_mode

@dataclass(frozen=True)
class CacheExecutionBinding:
    mechanism: CacheMechanism | None
    decision_reason: str
    context_plan_hash: str
    plan_hash: str
    binding_version: int = 1
    resource_id: str | None = None
    resource_generation: int | None = None
    provider_handle: str | None = None
    expires_at: str | None = None
    breakpoint_anchors: tuple[dict[str, Any], ...] = ()
    prefix: dict[str, Any] = field(default_factory=dict)
    material_binding_hash: str | None = None
    projection_version: str = "relay-cache-projection/1"
    metadata: dict[str, Any] = field(default_factory=dict)
    explicit_cache_material_mode: ExplicitCacheMaterialMode = 'inline_bytes_only'

    def __post_init__(self):
        explicit_cache_material_mode({'explicit_cache_material_mode':self.explicit_cache_material_mode})

    def canonical(self, *, include_provider_handle: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": "relay-cache-binding/1",
            "mechanism": self.mechanism,
            "decision_reason": self.decision_reason,
            "context_plan_hash": self.context_plan_hash,
            "plan_hash": self.plan_hash,
            "binding_version": self.binding_version,
            "resource_id": self.resource_id,
            "resource_generation": self.resource_generation,
            "expires_at": self.expires_at,
            "breakpoint_anchors": list(self.breakpoint_anchors),
            "prefix": self.prefix,
            "material_binding_hash": self.material_binding_hash,
            "projection_version": self.projection_version,
            "metadata": self.metadata,
        }
        if include_provider_handle:
            value["provider_handle"] = self.provider_handle
        if self.explicit_cache_material_mode!='inline_bytes_only':
            value['explicit_cache_material_mode']=self.explicit_cache_material_mode
        return value

    @property
    def binding_hash(self) -> str:
        return stable_hash(self.canonical(include_provider_handle=True))

    def public_summary(self) -> dict[str, Any]:
        return {
            "execution_mechanism": self.mechanism,
            "decision_reason": self.decision_reason,
        }

