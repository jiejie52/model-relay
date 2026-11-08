"""Equivalence rules for Relay-owned structure. Literal strings and arrays stay intact."""
from __future__ import annotations

import json
from typing import Any

from app.core.idempotency import canonical_json_bytes


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


def normalize_native_payload(payload: dict[str, Any], *, unordered_tools: bool = False,
                             response_schemas: bool = False) -> dict[str, Any]:
    """Canonicalize declared tool schemas while retaining tool ordering by default."""
    result = canonical_structure(payload) if response_schemas else dict(payload)
    if response_schemas:
        for path in (("generationConfig", "responseSchema"), ("generationConfig", "responseJsonSchema"),
                     ("output_config", "format", "schema"), ("response_format", "json_schema", "schema"),
                     ("text", "format", "schema")):
            node = result
            for key in path[:-1]:
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, dict) and isinstance(node.get(path[-1]), dict):
                node[path[-1]] = canonical_schema(node[path[-1]])
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
            declarations = normalized["functionDeclarations"]
            if response_schemas and unordered_tools and all(isinstance(d, dict) for d in declarations):
                names = [d.get("name") for d in declarations]
                if all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names):
                    normalized["functionDeclarations"] = sorted(declarations, key=lambda d: d["name"])
        return normalized

    tools = [normalize_tool(tool) for tool in result["tools"]]
    if unordered_tools and all(isinstance(tool, dict) for tool in tools):
        names = [tool.get("name") or (tool.get("function") or {}).get("name")
                 if isinstance(tool.get("function"), (dict, type(None))) else None for tool in tools]
        if all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names):
            tools = [tool for _, tool in sorted(zip(names, tools), key=lambda item: item[0])]
    result["tools"] = tools
    return result


def serialize_wire(payload: dict[str, Any], *, system_instruction_first: bool = True) -> bytes:
    """Serialize the native JSON without touching any model-visible literal."""
    canonical = canonical_structure(payload)
    if system_instruction_first and "systemInstruction" in canonical:
        canonical = {"systemInstruction": canonical["systemInstruction"],
                     **{key: value for key, value in canonical.items() if key != "systemInstruction"}}
        return json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return canonical_json_bytes(canonical)


def freeze_history_turn(entry: dict[str, Any]) -> dict[str, Any]:
    """Freeze native turn blocks once; timestamps/request IDs never enter them.

    The existing transport_history is the authoritative immutable representation,
    not a second rendered copy. Hashes bind that representation and each native
    block. A corrupted frozen turn fails before dispatch rather than being repaired.
    """
    from app.core.idempotency import stable_hash

    result = canonical_structure(entry)
    transport = result.get("transport_history")
    if not isinstance(transport, dict):
        return result
    blocks = []
    for field in ("user_content", "model_content", "user_message", "assistant_message", "user_item", "response_output"):
        if field in transport:
            value = transport[field]
            blocks.append({"field": field, "semantic_hash": stable_hash(value),
                           "wire_sha256": stable_hash(value),
                           "raw_byte_size": len(canonical_json_bytes(value))})
    frozen = {"format": "relay-frozen-turn/1", "transport_hash": stable_hash(transport), "blocks": blocks}
    prior = entry.get("_relay_context_freeze")
    if prior is not None and prior != frozen:
        raise ValueError("Frozen history transport or native blocks changed")
    result["_relay_context_freeze"] = frozen
    return result


def freeze_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [freeze_history_turn(turn) for turn in history]
