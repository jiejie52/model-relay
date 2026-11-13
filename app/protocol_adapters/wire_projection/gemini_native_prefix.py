"""Layout/10: cache only complete, immutable native conversation turns."""
import copy
import json
from app.core.idempotency import stable_hash
from app.core.material_placement import verify_placement
from app.protocol_adapters.wire_projection.gemini_wire import project_history_contents, project_current_user_content, project_system_instruction

LAYOUT = "gemini-physical-cache-layout/10"
PROJECTOR = "gemini-physical-projector/10"


def build_native_prefix_plan(*, snapshot, session, history, material_ids, material_bindings, context_plan,
                             placement_session=None, placement_history=None):
    placement = verify_placement(snapshot["material_placement_plan"],
        session=placement_session if placement_session is not None else session,
        history=placement_history if placement_history is not None else history)
    native = project_history_contents(history)
    current = project_current_user_content(snapshot, material_ids=material_ids, material_bindings=material_bindings)
    system = project_system_instruction(snapshot)
    if system is None and session.get("frozen_instructions"):
        system = {"parts": [{"text": str(session["frozen_instructions"])}]}
    full = {"contents": native + [current]}
    if system is not None:
        full["systemInstruction"] = system
    # Tool declarations are stable cache content only when the frozen contract
    # explicitly authorizes stripping them from cached inference.
    native_options = snapshot.get("provider_payload") or {}
    tools = native_options.get("tools") or snapshot.get("tools")
    tool_config = native_options.get("toolConfig") or snapshot.get("tool_config")
    if tools:
        full["tools"] = tools
    if tool_config:
        full["toolConfig"] = tool_config
    off = snapshot.get("requested_cache_mode") == "off"
    count = len(history)
    prefixes, suffixes, fingerprints, implicit_fingerprints = {}, {}, {}, {}
    scope = {k: placement["identity"][k] for k in ("session_id", "connection_id", "account_scope_id", "connection_spec_hash")}
    def fingerprint(payload):
        return stable_hash({"layout": LAYOUT, "model": snapshot.get("upstream_model") or snapshot["model"],
            "scope": scope, "prefix": payload})
    file_mode = (snapshot.get("cache_contract") or {}).get("explicit_cache_material_mode")
    cacheable = False
    reason = "cache_off" if off else "no_cacheable_contents"
    max_version = 0
    if not off:
        for version in range(count + 1):
            prefix = {"contents": native[:2 * version]}
            if system is not None:
                prefix["systemInstruction"] = system
            implicit_fingerprints[str(version)] = fingerprint(prefix)
            # No part is moved or converted to create a cache candidate. If an
            # inline-only supply cannot consume the native prefix, it remains
            # eligible for its authorized implicit track or uncached execution.
            has_file = any("fileData" in p for c in prefix["contents"] for p in c["parts"])
            allowed = (version > 0 and (not has_file or file_mode == "file_uri_allowed")
                and not tools and not tool_config)
            body_bytes = len(json.dumps(prefix, ensure_ascii=False, separators=(",", ":")).encode())
            if not allowed or body_bytes > 99_000_000:
                if has_file and file_mode != "file_uri_allowed":
                    reason = "native_prefix_file_reference_not_authorized"
                continue
            prefixes[str(version)] = prefix
            suffixes[str(version)] = {"contents": native[2 * version:] + [current]}
            fingerprints[str(version)] = fingerprint(prefix)
            max_version = version
        cacheable = bool(prefixes)
        if cacheable:
            reason = "native_prefix_slice"
    prefix = prefixes.get(str(max_version), {})
    suffix = suffixes.get(str(max_version), copy.deepcopy(full))
    occurrences = [{"source": "history_entry", "history_entry_index": i,
        "placement": "cached_prefix" if i < max_version else "uncached_suffix"} for i in range(count)]
    plan = {"schema_version": "relay-gemini-physical-cache-plan/9", "layout_version": LAYOUT,
        "projector_version": PROJECTOR, "model": snapshot.get("upstream_model") or snapshot["model"],
        "cached_prefix": prefix, "uncached_suffix": suffix,
        "uncached_suffix_by_prefix_version": suffixes,
        "uncached_suffix_wire_hashes": {k: stable_hash(v) for k, v in suffixes.items()},
        "full_uncached_payload": full, "history_entry_user_content": current,
        "implicit_prefix_payload": copy.deepcopy(full), "implicit_prefix_measurement_payload": {**({"contents": native} if native else {}), **({"systemInstruction": system} if system is not None else {})},
        "implicit_prefix_eligible": not off and bool(native or system),
        "implicit_prefix_fingerprint": implicit_fingerprints.get(str(count), ""),
        "implicit_prefix_compatible_fingerprints": implicit_fingerprints,
        "compatible_prefix_fingerprints": fingerprints,
        "occurrence_mapping": occurrences, "current_material_occurrences": [],
        "cache_spec": {"schema_version": "relay-gemini-cache-spec/10", "layout_version": LAYOUT},
        "cacheable": cacheable, "cacheability_reason": reason,
        "content_fingerprint": fingerprints.get(str(max_version), ""),
        "reuse_key": stable_hash({"scope": scope, "layout": LAYOUT,
            "model": snapshot.get("upstream_model") or snapshot["model"], "system": system}),
        "prefix_version": max_version, "measurement_order": "provider_create", "cache_create_transport": "google_genai_sdk",
        "cache_action": "bypass_off" if off else "native_prefix_slice",
        "dependencies": {"history_version": count, "history_entries": count,
            "safe_history_prefix_version": count, "history_cached_prefix_version": max_version,
            "context_plan_hash": context_plan["context_plan_hash"], "projection_safe": True,
            "cache_material_ids": [], "inference_only_material_ids": list(material_ids),
            "cache_create_request_bytes": len(json.dumps(prefix, ensure_ascii=False).encode()),
            "cache_create_max_request_bytes": 100_000_000,
            "placement_plan_digest": placement["plan_digest"]}}
    for field, value in (("cached_prefix_wire_hash", prefix), ("uncached_suffix_wire_hash", suffix),
            ("full_uncached_wire_hash", full), ("implicit_prefix_payload_hash", full), ("occurrence_mapping_hash", occurrences)):
        plan[field] = stable_hash(value)
    plan["physical_plan_hash"] = stable_hash(plan)
    return plan
