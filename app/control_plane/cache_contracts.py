from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from app.core.idempotency import stable_hash

CacheMode = Literal["off", "auto", "on"]


CacheMechanism = Literal["stateful_resource", "breakpoint", "implicit_prefix"]


CacheResolutionStatus = Literal["pending", "finalized"]


def provider_managed_trial_enabled(config: dict[str, Any]) -> bool:
    """An explicit control-plane grant; no inferred threshold or certification."""
    return (
        config.get("verification_status") == "trial"
        and config.get("auto_trial_enabled") is True
        and config.get("threshold_mode") == "provider_managed_trial"
    )

