from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from app.immutable import freeze_json, thaw_json

from app.shared_connection_contract import frozen_connection_fields


def dispatch_input_digest(*, snapshot, session, history, material_ids, material_bindings,
                          tenant_id, conversation_hash, request_id, session_id, context_plan,
                          cache_execution_binding, execution_fence, provider_physical_plan, affinity_headers,
                          connection_spec_hash=None,account_scope_id=None):
    from app.core.idempotency import stable_hash
    spec,scope=frozen_connection_fields(snapshot,(session.get('metadata') or {}).get('_relay_route'))
    if connection_spec_hash is not None and connection_spec_hash!=spec or account_scope_id is not None and account_scope_id!=scope:
        raise ValueError('Dispatch identity differs from frozen snapshot')
    return stable_hash({**({'connection_spec_hash':spec,'account_scope_id':scope} if spec else {}),"schema_version": "relay-prepared-execution/2",
        "snapshot": {k: v for k, v in snapshot.items() if k != "_relay_cache_execution"},
        "session": session, "history": history, "material_ids": material_ids,
        "material_bindings": material_bindings, "tenant_id": tenant_id,
        "conversation_hash": conversation_hash, "request_id": request_id, "session_id": session_id,
        "context_plan": context_plan, "cache_binding": cache_execution_binding,
        "execution_fence": execution_fence, "provider_physical_plan": provider_physical_plan,
        "affinity_headers": affinity_headers})

@dataclass(frozen=True)
class SealedDispatchBundle:
    snapshot: dict[str, Any]
    session: dict[str, Any]
    history: list[dict[str, Any]]
    material_ids: list[str]
    material_bindings: list[dict[str, Any]]
    tenant_id: str
    conversation_hash: str
    request_id: str | None = None
    session_id: str | None = None
    context_plan: dict[str, Any] | None = None
    cache_execution_binding: dict[str, Any] | None = None
    execution_fence: dict[str, Any] | None = None
    provider_physical_plan: dict[str, Any] | None = None
    seal_id: str = ""
    seal_hash: str = ""
    affinity_headers: dict[str, str] = field(default_factory=dict)
    connection_spec_hash: str = field(default='',kw_only=True)
    account_scope_id: str = field(default='',kw_only=True)

    def __post_init__(self):
        spec,scope=frozen_connection_fields(self.snapshot,(self.session.get('metadata') or {}).get('_relay_route'))
        if not isinstance(self.connection_spec_hash,str) or not isinstance(self.account_scope_id,str):raise TypeError('Connection identity fields must be strings')
        if self.connection_spec_hash and self.connection_spec_hash!=spec or self.account_scope_id and self.account_scope_id!=scope:
            raise ValueError('Sealed bundle connection identity mismatch')
        object.__setattr__(self,'connection_spec_hash',spec)
        object.__setattr__(self,'account_scope_id',scope)
        for name in ("snapshot", "session", "history", "material_ids", "material_bindings", "context_plan", "cache_execution_binding", "execution_fence", "provider_physical_plan", "affinity_headers"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))

    @property
    def route_binding(self): return (self.session.get("metadata") or {}).get("_relay_route") or {}
    @property
    def effective_semantics(self):
        return freeze_json({k: self.snapshot.get(k) for k in ("think_level", "effective_options", "structured_output", "requested_cache_mode")})
    @property
    def material_binding(self): return self.material_bindings
    @property
    def cache_binding(self): return self.cache_execution_binding

    def verify_integrity(self):
        from app.core.idempotency import stable_hash
        from app.shared_contracts import ExecutionConflictError
        if not self.seal_id or not self.seal_hash or self.seal_id != self.request_id:
            raise ExecutionConflictError("Adapter dispatch requires an identified execution seal")
        if ("_relay_cache_execution" in self.snapshot
                and self.snapshot["_relay_cache_execution"] != self.cache_execution_binding):
            raise ExecutionConflictError("Snapshot cache projection differs from the sealed cache binding")
        physical = self.provider_physical_plan
        if physical is not None and physical.get("physical_plan_hash") != stable_hash(
                {k: v for k, v in physical.items() if k != "physical_plan_hash"}):
            raise ExecutionConflictError("Physical plan content differs from its digest")
        actual = dispatch_input_digest(**{name: getattr(self, name) for name in (
            "snapshot", "session", "history", "material_ids", "material_bindings", "tenant_id",
            "conversation_hash", "request_id", "session_id", "context_plan", "cache_execution_binding",
            "execution_fence", "provider_physical_plan", "affinity_headers","connection_spec_hash","account_scope_id")})
        if actual != self.seal_hash:
            raise ExecutionConflictError("Sealed adapter inputs do not match the atomic dispatch digest")
        return True

@dataclass(frozen=True)
class ActualExecutionFact:
    request_id: str
    provider_dispatch_state: Literal["result_stored", "dispatch_started", "not_sent"]
    material_binding: list[dict[str, Any]] = field(default_factory=list)
    cache_binding: dict[str, Any] | None = None
    seal_record: dict[str, Any] = field(default_factory=dict)
    standardized_result: Any = None
    provisional_raw_object_id: str | None = None
    provisional_output_object_id: str | None = None
    raw_error_envelope: dict[str, Any] | None = None
    failure_stage: str | None = None
    execution_observation: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("material_binding", "cache_binding", "seal_record", "raw_error_envelope", "execution_observation"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))

@dataclass
class RuntimeState:
    """Private working state; never passed across a layer boundary."""
    plan: Any
    request_row: dict
    session: dict
    snapshot: dict
    history: list
    request_id: str
    session_id: str
    material_ids: list
    native_gemini: bool
    is_v3: bool
    fence: Any
    upgrade: dict
    execution_started_ms: float
    wire: dict = field(default_factory=dict)
    rotated_affinity: bool = False
    material_bindings: list = field(default_factory=list)
    context_plan: Any = None
    provider_physical_plan: Any = None
    cache_material_projection: Any = None
    material_reuse_observation: Any = None
    cache_binding: Any = None
    cache_binding_obj: Any = None
    execution_snapshot: dict = field(default_factory=dict)
    sealed_bundle: Any = None
    failure_stage: str = "pre_dispatch_check"

    @classmethod
    def from_plan(cls, plan, started):
        row, session, snapshot = map(thaw_json, (plan.request_record, plan.session_record, plan.snapshot))
        ids = list(snapshot.get("material_ids") or [])
        if session.get("context_policy") == "conversation": ids = list(session.get("material_manifest") or []) + ids
        return cls(plan, row, session, snapshot, thaw_json(plan.history), str(row["id"]), str(row["session_id"]),
                   list(dict.fromkeys(ids)), snapshot.get("provider")=="gemini" and snapshot.get("protocol", "gemini_native")=="gemini_native",
                   snapshot.get("schema_version") in {"relay-request/2.3", "relay-request/2.4"}, plan.execution_fence, snapshot.get("cache_upgrade") or {}, started)
