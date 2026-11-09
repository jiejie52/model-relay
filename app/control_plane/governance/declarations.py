"""Offline declarations and supply-specific certification admission.
Evidence records/approval authentication belong to the controlled deployment workflow.
No arbitrary code, wire template, network probe or secret may enter a release.
"""
from datetime import datetime, timezone
import json
from app.control_plane.definitions import ModelControlPlane
from app.core.idempotency import stable_hash

ENTITY_PARSERS = {"connections": "_connection_from_mapping", "capability_contracts": "_contract_from_mapping",
    "protocol_profiles": "_profile_from_mapping", "cache_policies": "_cache_policy_from_mapping"}
ID_FIELDS = {"connections": "connection_id", "capability_contracts": "contract_id", "protocol_profiles": "profile_id", "cache_policies": "policy_id"}
FORBIDDEN = {"api_key", "apikey", "secret", "access_token", "authorization", "python_class", "class_path", "script", "wire_template"}

def safe_declaration(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k.lower() in FORBIDDEN: raise ValueError("Secrets and executable configuration are forbidden")
            safe_declaration(v)
    elif isinstance(value, list):
        for item in value: safe_declaration(item)

def merge_entities(base, declaration, settings):
    safe_declaration(declaration)
    parts = {}
    for name, parser in ENTITY_PARSERS.items():
        items = dict(getattr(base, name))
        seen = set()
        for item in declaration.get(name, []):
            identity = item.get(ID_FIELDS[name])
            if not identity or identity in seen: raise ValueError("Missing or duplicate declaration identity")
            seen.add(identity)
            parsed = getattr(ModelControlPlane, parser)(item)
            items[identity] = parsed
        parts[name] = items
    offerings = dict(base.offerings)
    seen = set()
    for item in declaration.get("offerings", []):
        s = ModelControlPlane._offering_from_mapping(item, settings.deployment_id)
        if s.offering_id in seen: raise ValueError("Duplicate supply identity")
        seen.add(s.offering_id)
        offerings[s.offering_id] = s
    return ModelControlPlane(revision=declaration.get("revision", base.revision),
        **{k: v.values() for k,v in parts.items()}, offerings=offerings.values(),
        release_metadata={"source": "declaration-staging"})

def certification_identity(plane, supply):
    connection = plane.connection(supply.connection_id)
    contract = plane.contract(supply.capability_contract_id)
    profile = plane.protocol_profile(supply.protocol_profile_id)
    policy = plane.cache_policy(supply.cache_policy_id)
    return {"connection": connection.canonical(), "capability": contract.canonical(),
        "profile": profile.canonical(), "cache_policy": policy.canonical() if policy else None,
        "provider": supply.provider, "model": supply.model_pattern,
        "upstream_model": supply.metadata.get("upstream_model", supply.model_pattern),
        "execution_authorization": supply.metadata.get("execution_authorization", {})}


def certification_request(declaration, *, settings):
    plane = merge_entities(ModelControlPlane._builtin(settings), declaration, settings)
    return {row["offering_id"]: {"status": "draft", "supply_hash": stable_hash(certification_identity(plane, plane.offerings[row["offering_id"]])),
        "evidence_ref": None, "reviewed_by": None, "valid_until": None,
        "capabilities": sorted({"inference", *plane.contract(row["capability_contract_id"]).cache.get("supported_mechanisms", []),
            *plane.contract(row["capability_contract_id"]).metadata.get("material_transport", {}).get("supported_material_transports", []),
            *(["file_uri_allowed"] if plane.contract(row["capability_contract_id"]).cache.get("explicit_cache_material_mode") == "file_uri_allowed" else [])})}
        for row in declaration.get("offerings", [])}


def validate_supply(plane, supply, evidence):
    from app.protocol_adapters.factory import RESOURCE_MECHANISMS, IMPLEMENTED_PROTOCOLS
    contract = plane.contract(supply.capability_contract_id)
    profile = plane.protocol_profile(supply.protocol_profile_id)
    connection = plane.connection(supply.connection_id)
    if not connection or not contract or not profile or connection.protocol != profile.protocol:
        raise ValueError("Supply references inconsistent connection/capability/profile")
    if profile.protocol not in IMPLEMENTED_PROTOCOLS: raise ValueError("Protocol implementation is unavailable")
    if profile.metadata.get("adapter_type", profile.protocol) != profile.protocol:
        raise ValueError("Profile adapter_type is not a registered implementation")
    import re
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", connection.credential_ref or ""):
        raise ValueError("Invalid credential reference")
    if connection.api_version and not re.fullmatch(r"v[0-9]+(?:alpha|beta)?[0-9]*", connection.api_version):
        raise ValueError("Invalid API version")
    if set(connection.sdk_endpoint_options)-{"base_url", "api_version"}:
        raise ValueError("SDK endpoint option has no supported interpretation")
    if connection.api_version and connection.sdk_endpoint_options.get("api_version", connection.api_version) != connection.api_version:
        raise ValueError("REST and SDK API versions disagree")
    if connection.auth_type not in {"bearer_header", "x_goog_api_key_header", "x_api_key_header"}:
        raise ValueError("Managed connection requires a registered authentication type")
    if connection.protocol in {"responses", "chat_completions", "moonshot_chat"} and connection.auth_type != "bearer_header":
        raise ValueError("This protocol implementation requires bearer_header")
    if connection.protocol == "claude_messages" and connection.auth_type != "x_api_key_header":
        raise ValueError("Claude implementation requires x_api_key_header")
    if connection.protocol == "gemini_native" and connection.auth_type != "x_goog_api_key_header":
        raise ValueError("Gemini SDK implementation requires x_goog_api_key_header")
    if not connection.credential_ref or not connection.account_id or not connection.region:
        raise ValueError("Managed connection requires credential_ref, account_id and region")
    if not (connection.rest_base_url or connection.base_url): raise ValueError("Missing connection endpoint")
    from urllib.parse import urlsplit
    for endpoint in (connection.rest_base_url or connection.base_url, connection.upload_base_url,
                     connection.sdk_endpoint_options.get("base_url")):
        if endpoint:
            url = urlsplit(endpoint)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("Invalid managed endpoint")
            if url.hostname not in profile.metadata.get("allowed_hosts", []): raise ValueError("Endpoint host is outside Profile")
    affinity = profile.metadata.get("affinity_policy")
    if not isinstance(affinity, dict) or type(affinity.get("enabled")) is not bool:
        raise ValueError("Managed Profile must explicitly declare affinity policy")
    if affinity["enabled"] and (not affinity.get("header") or not affinity.get("allowed_hosts") or
            not set(affinity["allowed_hosts"]).issubset(profile.metadata["allowed_hosts"])):
        raise ValueError("Invalid affinity header/host policy")
    if affinity["enabled"]:
        from string import Formatter
        fields = [field for _, field, _, _ in Formatter().parse(affinity.get("value_template", "{token}")) if field]
        if any(field not in {"token", "epoch", "session_id"} for field in fields): raise ValueError("Invalid affinity format template")
    if profile.protocol == "gemini_native":
        thinking = contract.thinking
        if thinking.get("wire_strategy") not in {"gemini_thinking_level", "gemini_thinking_budget", None}:
            raise ValueError("Gemini thinking strategy has no implementation")
        if thinking.get("wire_strategy") == "gemini_thinking_budget":
            budgets = thinking.get("budget_map", {})
            if any(type(budgets.get(level)) is not int or budgets[level] < -1 for level in thinking.get("accepted_levels", [])):
                raise ValueError("Gemini thinking budget map is incomplete")
        if set(contract.supported_options)-{"temperature", "top_p", "max_output_tokens"}:
            raise ValueError("Gemini option has no Native Wire projection")
    declared = set(contract.cache.get("supported_mechanisms", []))
    if declared and contract.cache.get("verification_status") not in {"verified", "published"}:
        raise ValueError("Enabled cache contract is not verified")
    mapped = set(profile.cache.get("supported_mechanisms", []))
    implemented = RESOURCE_MECHANISMS.get(profile.protocol, set())
    if not declared.issubset(mapped & implemented): raise ValueError("Cache capability/Profile/runtime intersection is incomplete")
    policy = plane.cache_policy(supply.cache_policy_id)
    if declared and (not policy or policy.scope != "session" or policy.cross_session_sharing):
        raise ValueError("Cache resources must be Session-private")
    if policy:
        if policy.metadata.get("default_mode", "auto") not in {"off", "auto", "on"}:
            raise ValueError("Invalid default cache mode")
        if type(policy.metadata.get("allow_force", True)) is not bool:
            raise ValueError("Cache force authorization must be boolean")
        if policy.metadata.get("default_mode") == "on" and policy.metadata.get("allow_force") is False:
            raise ValueError("Default force mode is not authorized")
    for value in (contract.cache, profile.cache, policy.metadata if policy else {}):
        if any(value.get(k) for k in ("heartbeat_keepalive", "implicit_keepalive", "background_renewal", "orphan_cleanup")):
            raise ValueError("Requested background capability has no implemented maintenance worker")
    transport = contract.metadata.get("material_transport", {})
    modes = set(transport.get("supported_material_transports", []))
    if not modes.issubset({"provider_files_api", "external_signed_url"}):
        raise ValueError("Material transport mode has no implemented binder")
    if "external_signed_url" in modes and int(transport.get("external_url_max_total_bytes", -1)) < 0:
        raise ValueError("External URL transport needs an explicit aggregate threshold")
    file_mode = contract.cache.get("explicit_cache_material_mode", "inline_bytes_only")
    if file_mode not in {"inline_bytes_only", "file_uri_allowed"}: raise ValueError("Unknown cache material mode")
    required = {"inference", *declared, *modes}
    if file_mode == "file_uri_allowed":
        if profile.protocol != "gemini_native" or "provider_files_api" not in modes or "stateful_resource" not in declared:
            raise ValueError("File URI caching needs independently supported file transport and stateful cache")
        required.add("file_uri_allowed")
    if not evidence or evidence.get("status") != "passed" or not all(evidence.get(k) for k in ("evidence_ref", "reviewed_by", "valid_until", "supply_hash")):
        raise ValueError("Enabled supply requires independently reviewed certification")
    identity = certification_identity(plane, supply)
    if stable_hash(identity) != evidence["supply_hash"]: raise ValueError("Certification does not match exact supply contracts")
    expiry = datetime.fromisoformat(evidence["valid_until"].replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc): raise ValueError("Certification is expired")
    authorization = supply.metadata.get("execution_authorization", {})
    headers = profile.metadata.get("response_evidence_headers", {})
    first_party=(authorization.get('evidence_source_mode')=='first_party_native_payload' and profile.metadata.get('evidence_source_mode')=='first_party_native_payload' and connection.protocol=='gemini_native')
    if authorization.get("require_native_constraint_evidence") and not first_party and not headers.get("native_constraint_enforced"):
        raise ValueError("Native evidence policy has no certified evidence mapping")
    if authorization.get("on_unknown_billing") == "reject" and authorization.get("expected_billing_tier") is not None and not first_party and not headers.get("billing_tier"):
        raise ValueError("Billing evidence policy has no certified evidence mapping")
    if not required.issubset(evidence.get("capabilities", [])): raise ValueError("Supply capability certification is incomplete")
    return evidence["evidence_ref"]

def build_declaration_candidate(declaration, *, settings, revision, certifications=None):
    from app.control_plane.governance.snapshot_compiler import digest
    base = ModelControlPlane._builtin(settings)
    plane = merge_entities(base, declaration, settings)
    groups, supplies, refs = {}, [], []
    certs = certifications or {}
    order = set()
    for row in declaration.get("offerings", []):
        supply = plane.offerings[row["offering_id"]]
        key = (supply.provider, supply.model_pattern, supply.deployment_id)
        if any(c in supply.model_pattern for c in "*?"): raise ValueError("Managed supplies require exact model identities")
        groups[key] = dict(zip(("provider", "model", "deployment_id"), key))
        value = supply.canonical()
        value["metadata"] = {**value["metadata"], "managed_v2": True,
            "unavailable_action": value["metadata"].get("unavailable_action", "try_next")}
        if supply.enabled:
            ref = validate_supply(plane, supply, certs.get(supply.offering_id))
            rank = (key, supply.priority)
            if rank in order: raise ValueError("Duplicate channel priority in managed model group")
            order.add(rank)
            value["priority"] = 10000 - supply.priority
            value["metadata"].update(certification_evidence_ref=ref, declared_priority=supply.priority)
            refs.append(ref)
        supplies.append(value)
    return {"status": "review", "mode": "merge", "revision": revision,
        **{name: [getattr(plane, name)[x[ID_FIELDS[name]]].canonical() for x in declaration.get(name, [])] for name in ENTITY_PARSERS},
        "offerings": supplies, "release_metadata": {"source": "supply-declarations", "base_control_plane_hash": base.control_plane_hash,
            "declarations_sha256": digest(declaration), "managed_model_groups": [groups[k] for k in sorted(groups)],
            "approved_supply_evidence_refs": sorted(set(refs)), "certifications_sha256": digest(certs)}}
