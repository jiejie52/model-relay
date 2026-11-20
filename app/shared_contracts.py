from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from app.core.idempotency import stable_hash
from app.control_plane.cache_contracts import CacheMode, CacheMechanism, CacheResolutionStatus

class CacheDecisionError(RuntimeError):
    def __init__(self, code: str, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason

    def public_detail(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.reason:
            value["reason"] = self.reason
        return value


@dataclass(frozen=True)
class ExecutionFence:
    owner: str
    epoch: int
    deadline: str | None = None
    kind: Literal["async_job", "sync_request"] = "async_job"

    def canonical(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "epoch": self.epoch,
            "deadline": self.deadline,
            "kind": self.kind,
        }


from app.immutable import freeze_json, thaw_json

class ExecutionConflictError(RuntimeError):
    """A stale executor or history conflict cannot publish execution facts."""
