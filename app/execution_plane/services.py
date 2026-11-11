from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Mapping

class ExecutionRequestError(ValueError):
    """Transport-neutral request rejection, mapped to HTTP by the API boundary."""
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code, self.detail = status_code, detail

@dataclass(frozen=True)
class ExecutionServices:
    repo: Any
    settings: Any
    storage: Any
    route_resolver: Any
    provider_files: Any
    inline_scheduler: Any
    headers: Mapping[str, str] = field(default_factory=dict)
    entry_point: str = "/v2/sessions/{session_id}/requests"
