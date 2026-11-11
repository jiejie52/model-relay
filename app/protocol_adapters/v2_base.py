from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class StandardizedProviderResult:
    raw_bytes: bytes
    raw_json: dict[str, Any]
    text: str
    response_id: str | None
    usage: dict[str, Any]
    cached_tokens: int | None
    response_output: Any
    history_entry: dict[str, Any]
    http_status: int | None = None
    provider_request_id: str | None = None
    # Facts observed from the upstream success response. They are distinct from
    # caller-requested and Relay-effective semantics and are used for audit /
    # contract checks without changing execution identity after dispatch.
    observed: dict[str, Any] | None = None


    normalized_usage: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        from app.immutable import freeze_json
        from app.protocol_adapters.thinking_evidence import extract_thinking_evidence
        observed = dict(self.observed or {})
        observed.setdefault("observed_thinking", extract_thinking_evidence(self.raw_json, observed.get("protocol"), observed))
        object.__setattr__(self, "observed", observed)
        from app.protocol_adapters.cache_usage import normalize_cache_usage
        if not self.normalized_usage:
            normalized = normalize_cache_usage(
                protocol=(self.observed or {}).get("protocol"), usage=self.usage,
                effective_mechanism=None, requested_mode="auto").canonical()
            raw_usage = self.usage.get("raw") if isinstance(self.usage.get("raw"), dict) else self.usage
            normalized["input_tokens"] = normalized.get("prompt_tokens_total")
            normalized["output_tokens"] = next((value for value in (
                self.usage.get("output_tokens"), self.usage.get("completion_tokens"),
                raw_usage.get("candidatesTokenCount"), raw_usage.get("candidates_token_count"))
                if type(value) is int and value >= 0), None)
            object.__setattr__(self, "normalized_usage", normalized)
        for name in ("raw_json", "usage", "response_output", "history_entry", "observed", "normalized_usage"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))

    @property
    def provider_response_id(self): return self.response_id
    @property
    def actual_model(self): return (self.observed or {}).get("actual_model")
    @property
    def visible_text(self): return self.text
    @property
    def raw_response_bytes(self): return self.raw_bytes
    @property
    def raw_response_content_type(self): return "application/json"
    @property
    def response_output_items(self): return self.response_output
    @property
    def observed_routing_metadata(self): return self.observed or {}
    @property
    def structured_payload(self):
        import json
        try: return json.loads(self.text)
        except (ValueError, TypeError): return None

from app.runtime.contracts import SealedDispatchBundle
V2ProviderResult = StandardizedProviderResult
V2ExecutionContext = SealedDispatchBundle

class ProtocolAdapter(Protocol):
    adapter_version: str
    async def execute(self, context: SealedDispatchBundle) -> StandardizedProviderResult: ...

V2ProviderAdapter = ProtocolAdapter
