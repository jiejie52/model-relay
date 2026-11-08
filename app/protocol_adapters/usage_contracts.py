from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from app.core.idempotency import stable_hash
from app.control_plane.cache_contracts import CacheMode, CacheMechanism, CacheResolutionStatus

@dataclass(frozen=True)
class CacheUsageObservation:
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    uncached_input_tokens: int | None
    actual_cache_hit_status: str
    evidence_level: str
    attribution: str = "provider_reported"
    transparent_observation: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    prompt_tokens_total: int | None = None
    cache_read_ratio: float | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "prompt_tokens_total": self.prompt_tokens_total,
            "cache_read_tokens": self.cache_read_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_read_ratio": self.cache_read_ratio,
            "cache_write_tokens": self.cache_write_tokens,
            "actual_cache_hit_status": self.actual_cache_hit_status,
            "evidence_level": self.evidence_level,
            "attribution": self.attribution,
            "transparent_observation": self.transparent_observation,
            "raw": self.raw,
        }

