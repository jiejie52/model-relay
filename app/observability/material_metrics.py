"""Material, schema and native Usage attribution without content logging."""
from app.core.idempotency import canonical_json_bytes
from app.core.material_placement import placement_summary


def material_metrics(snapshot, physical, historical_bindings, result=None, cache_binding=None):
    placement = snapshot.get("material_placement_plan")
    if not placement:
        return {}
    full = (physical or {}).get("full_uncached_payload") or {}
    contents = full.get("contents") or []
    def files(messages):
        return sum(any(k in p for k in ("fileData", "inlineData")) for c in messages for p in c.get("parts") or [])
    def text_size(messages):
        return sum(len(str(p["text"]).encode()) for c in messages for p in c.get("parts") or [] if "text" in p)
    from app.canonical_schema import resolve_structured_output
    spec = resolve_structured_output(snapshot)
    schema = spec.schema if spec is not None and spec.schema is not None else {}
    projected_bytes = 0
    if schema:
        from app.protocol_adapters.wire_projection.schema_projector import project_schema_for_provider
        projected = project_schema_for_provider(schema, provider=snapshot.get("provider"), model=snapshot.get("model"))
        projected_bytes = len(canonical_json_bytes(projected.schema))
    raw = (result.raw_json or {}) if result else {}
    usage = raw.get("usageMetadata") or raw.get("usage_metadata") or {}
    wire_contents = contents
    if (cache_binding or {}).get("mechanism") == "stateful_resource":
        version = ((cache_binding or {}).get("metadata") or {}).get("cached_history_version")
        if version is None:
            version = ((cache_binding or {}).get("prefix") or {}).get("prefix_version")
        wire_contents = (((physical or {}).get("uncached_suffix_by_prefix_version") or {}).get(str(version))
            or (physical or {}).get("uncached_suffix") or {}).get("contents") or []
    return {**placement_summary(placement),
        "logical_total_file_part_count": files(contents),
        "historical_pinned_material_count": len({b["material_id"] for b in historical_bindings}),
        "wire_total_file_part_count": files(wire_contents),
        "wire_current_turn_file_part_count": files(wire_contents[-1:]),
        "history_content_count": max(0, len(contents) - 1),
        "history_text_bytes": text_size(contents[:-1]), "current_text_bytes": text_size(contents[-1:]),
        "canonical_schema_bytes": len(canonical_json_bytes(schema)) if schema else 0,
        "projected_schema_bytes": projected_bytes,
        "cache_action": (physical or {}).get("cache_action"),
        "provider_prompt_tokens": usage.get("promptTokenCount", usage.get("prompt_token_count")),
        "provider_cached_tokens": usage.get("cachedContentTokenCount", usage.get("cached_content_token_count")),
        "provider_prompt_tokens_details": usage.get("promptTokensDetails", usage.get("prompt_tokens_details"))}
