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

    def __post_init__(self):
        for name in ("request_record", "session_record", "effective_semantics", "context_plan", "cache_intent_plan", "snapshot", "history"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))
