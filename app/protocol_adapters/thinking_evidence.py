"""Extract observable thinking facts without inferring unreported effort/depth."""
from dataclasses import asdict
from app.thinking_contracts import ObservedThinkingEvidence


def extract_thinking_evidence(data, protocol, observed):
    tokens, blocks = None, None
    def count(v): return v if type(v) is int and v >= 0 else None
    if protocol == "gemini_native":
        usage = data.get("usageMetadata") or data.get("usage_metadata") or {}
        tokens = count(usage.get("thoughtsTokenCount", usage.get("thoughts_token_count")))
        candidates = data.get("candidates") or []
        if isinstance(candidates, (list, tuple)):
            parts = [p for c in candidates if isinstance(c, dict)
                     for p in (c.get("content") or {}).get("parts", []) if isinstance(p, dict)]
            if any(p.get("thought") is True for p in parts): blocks = True
    else:
        usage = data.get("usage") or {}
        detail = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
        tokens = count(detail.get("reasoning_tokens"))
        if tokens is None: tokens = count(usage.get("reasoning_tokens"))
        output = data.get("output") or data.get("content") or []
        if isinstance(output, (list, tuple)) and any(isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking", "reasoning"} for b in output):
            blocks = True
        choices = data.get("choices") or []
        if isinstance(choices, (list, tuple)) and any(isinstance(c, dict) and
                (c.get("message") or {}).get("reasoning_content") for c in choices): blocks = True
    return asdict(ObservedThinkingEvidence(tokens, blocks,
        observed.get("thinking_parameter_stripped"), observed.get("thinking_degraded"),
        observed.get("effective_thinking_mode"), observed.get("evidence_source_mode", "gateway_headers")))
