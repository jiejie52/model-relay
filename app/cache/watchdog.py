"""Measure the frozen stable text prefix before installing an implicit binding."""
from __future__ import annotations

from typing import Any

from .contracts import CacheDecisionError


def stable_text_prefix(snapshot: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if snapshot.get("instructions"):
        payload["systemInstruction"] = {"parts": [{"text": str(snapshot["instructions"])}]}
    contents = []
    for turn in history:
        transport = turn.get("transport_history") or {}
        if transport.get("kind") != "chat_completions":
            raise CacheDecisionError("CACHE_PREFIX_ASSESSMENT_UNKNOWN", "Stable history has no certified text projection")
        for key, role in (("user_message", "user"), ("assistant_message", "model")):
            message = transport.get(key) or {}
            if not isinstance(message.get("content"), str) or message.get("tool_calls"):
                raise CacheDecisionError("CACHE_PREFIX_ASSESSMENT_UNKNOWN", "Stable history is not a text-only prefix")
            contents.append({"role": role, "parts": [{"text": message["content"]}]})
    if contents:
        payload["contents"] = contents
    elif snapshot.get("instructions"):
        # Count only the real stable text. A count request does not dispatch
        # inference or append a conversation turn.
        payload = {"contents": [{"role": "user", "parts": [{"text": str(snapshot["instructions"])}]}]}
    return payload


async def assess_implicit_prefix(registry, snapshot, context_plan, history) -> tuple[int, str]:
    assessment = context_plan.get("token_assessment") or {}
    if assessment.get("status") == "measured" and type(assessment.get("lower")) is int:
        return assessment["lower"], str(assessment.get("source") or "frozen_measurement")
    payload = stable_text_prefix(snapshot, history)
    if not payload:
        return 0, "empty_stable_prefix"
    profile_cache = (snapshot.get("protocol_profile") or {}).get("cache") or {}
    connection = profile_cache.get("token_count_connection_id")
    adapter = registry.maybe_get(str(connection)) if registry is not None and connection else None
    if adapter is None:
        raise CacheDecisionError("CACHE_PREFIX_ASSESSMENT_UNKNOWN", "Frozen profile has no prefix token counter")
    try:
        count = await adapter.measure(spec={"model": snapshot["model"], "provider_payload": payload})
    except Exception as exc:
        raise CacheDecisionError("CACHE_PREFIX_ASSESSMENT_UNKNOWN", "Stable prefix measurement failed") from exc
    if type(count) is not int or count < 0:
        raise CacheDecisionError("CACHE_PREFIX_ASSESSMENT_UNKNOWN", "Prefix counter returned an invalid count")
    return count, "gemini_countTokens_stable_text_projection"
