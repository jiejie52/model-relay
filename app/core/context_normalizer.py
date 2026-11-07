"""Equivalence rules for Relay-owned structure. Literal strings and arrays stay intact."""
from __future__ import annotations

from typing import Any

from .idempotency import canonical_json_bytes


def canonical_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: canonical_structure(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical_structure(item) for item in value]
    return value


def canonical_schema(schema: Any) -> Any:
    """Normalize required only at known schema nodes, never in defaults/examples."""
    if not isinstance(schema, dict):
        return schema
    result = canonical_structure(schema)
    required = result.get("required")
    if isinstance(required, list) and all(isinstance(item, str) for item in required):
        result["required"] = sorted(set(required))
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        if isinstance(result.get(key), dict):
            result[key] = {name: canonical_schema(item) for name, item in result[key].items()}
    for key in ("items", "additionalProperties", "not", "if", "then", "else", "contains"):
        if isinstance(result.get(key), dict):
            result[key] = canonical_schema(result[key])
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(result.get(key), list):
            result[key] = [canonical_schema(item) for item in result[key]]
    return result


def normalize_structured_output(value: dict[str, Any]) -> dict[str, Any]:
    output = canonical_structure(value)
    for key in ("schema", "output_schema"):
        if isinstance(output.get(key), dict):
            output[key] = canonical_schema(output[key])
    return output


def source_markers(material_id: str, filename: str, *, upgraded: bool) -> tuple[str, str]:
    identity = f"{material_id}:{filename}" if upgraded else filename
    return f"[SOURCE_FILE:{identity}]", f"[END_SOURCE_FILE:{identity}]"


def normalize_native_payload(payload: dict[str, Any], *, unordered_tools: bool = False) -> dict[str, Any]:
    """Canonicalize declared tool schemas while retaining tool ordering by default."""
    result = dict(payload)
    if not isinstance(result.get("tools"), list):
        return result

    def normalize_tool(tool: Any) -> Any:
        if not isinstance(tool, dict):
            return tool
        normalized = dict(tool)
        for field in ("parameters", "input_schema", "parametersJsonSchema"):
            if isinstance(normalized.get(field), dict):
                normalized[field] = canonical_schema(normalized[field])
        if isinstance(normalized.get("function"), dict):
            normalized["function"] = normalize_tool(normalized["function"])
        if isinstance(normalized.get("functionDeclarations"), list):
            normalized["functionDeclarations"] = [
                normalize_tool(declaration) for declaration in normalized["functionDeclarations"]
            ]
        return normalized

    tools = [normalize_tool(tool) for tool in result["tools"]]
    if unordered_tools and all(isinstance(tool, dict) for tool in tools):
        names = [tool.get("name") or (tool.get("function") or {}).get("name")
                 if isinstance(tool.get("function"), (dict, type(None))) else None for tool in tools]
        if all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names):
            tools = [tool for _, tool in sorted(zip(names, tools), key=lambda item: item[0])]
    result["tools"] = tools
    return result


def serialize_wire(payload: dict[str, Any]) -> bytes:
    """Serialize the native JSON without touching any model-visible literal."""
    return canonical_json_bytes(payload)


def unified_cache_enabled(snapshot: dict[str, Any]) -> bool:
    """Only newly frozen 6.6 requests opt in. Never upgrade old recovery facts."""
    upgrade = snapshot.get("cache_upgrade") or {}
    policy = snapshot.get("cache_policy") or {}
    return (upgrade.get("version") == 2 and upgrade.get("grok_mode") != "legacy_v1"
            and snapshot.get("requested_cache_mode") != "off" and not policy.get("deny")
            and not (snapshot.get("requested_cache_mode") == "auto" and policy.get("auto_disabled")))


class SemanticEquivalenceNormalizer:
    """Normalize owned structure; never search/replace model-visible literals."""
    contract = {"version": "relay-equivalence/2", "literal_text": "preserved",
                "role_and_part_order": "preserved", "relay_ephemeral_metadata": "audit_only",
                "history": "scoped_frozen_native_fragments"}

    def normalize_request(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        result = dict(snapshot)
        result["input"] = canonical_structure(snapshot.get("input"))
        result["structured_output"] = normalize_structured_output(snapshot.get("structured_output") or {})
        return result


def normalize_wire(payload: dict[str, Any], *, protocol: str, unordered_tools: bool = False) -> dict[str, Any]:
    """One projection used by preparation and transport; literal text is immutable.

    Relay request IDs/timestamps stay in audit metadata, never in this content
    projection. No regex-based removal of caller text or user-owned JSON keys.
    """
    result = canonical_structure(normalize_native_payload(payload, unordered_tools=unordered_tools))
    if protocol == "gemini_native" and "systemInstruction" in result:
        result = {"systemInstruction": result["systemInstruction"],
                  **{k: v for k, v in result.items() if k != "systemInstruction"}}
    return result


def native_json_bytes(payload: dict[str, Any]) -> bytes:
    import json
    return json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":")).encode("utf-8")
