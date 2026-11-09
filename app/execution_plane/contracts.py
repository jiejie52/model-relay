from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from app.immutable import freeze_json
from app.shared_contracts import ExecutionFence

@dataclass(frozen=True)
class FrozenExecutionPlan:
    request_record: dict[str, Any]
    session_record: dict[str, Any]
    effective_semantics: dict[str, Any]
    context_plan: dict[str, Any]
    cache_intent_plan: dict[str, Any]
    execution_fence: ExecutionFence | None
    snapshot: dict[str, Any]
    history: list[dict[str, Any]] = field(default_factory=list)
    connection_spec_hash: str = field(default='',kw_only=True)
    account_scope_id: str = field(default='',kw_only=True)

    def __post_init__(self):
        from app.shared_connection_contract import frozen_connection_fields
        if not isinstance(self.connection_spec_hash,str) or not isinstance(self.account_scope_id,str):raise TypeError('Connection identity fields must be strings')
        spec,scope=frozen_connection_fields(self.snapshot,(self.session_record.get('metadata') or {}).get('_relay_route'))
        if self.connection_spec_hash and self.connection_spec_hash!=spec or self.account_scope_id and self.account_scope_id!=scope:
            raise ValueError('Frozen execution connection identity mismatch')
        object.__setattr__(self,'connection_spec_hash',spec)
        object.__setattr__(self,'account_scope_id',scope)
        for name in ("request_record", "session_record", "effective_semantics", "context_plan", "cache_intent_plan", "snapshot", "history"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))
