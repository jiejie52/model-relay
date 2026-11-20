"""Frozen pre-8.0 gateway observation rules."""
def legacy_response_facts(response_headers, channel_id, observed):
    from app.protocol_adapters.wire_projection.http_wire import _header_value, _header_bool
    if str(channel_id or "").lower() != "aihubmix":
        return observed

    final_model = _header_value(response_headers, "x-aihubmix-model")
    routed_model = _header_value(response_headers, "x-aihubmix-router-resolved-model")
    fallback = _header_bool(response_headers, "x-aihubmix-fallback")
    json_repaired = _header_bool(response_headers, "x-json-repaired")

    if final_model or routed_model:
        observed["actual_model"] = final_model or routed_model
    if final_model:
        observed["aihubmix_final_model"] = final_model
    if routed_model:
        observed["aihubmix_router_resolved_model"] = routed_model
    if fallback is not None:
        observed["aihubmix_fallback"] = fallback
    if json_repaired is not None:
        observed["json_repaired"] = json_repaired
    return observed
