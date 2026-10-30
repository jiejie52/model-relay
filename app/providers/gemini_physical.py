from __future__ import annotations

import copy
import json
from typing import Any

from ..core.idempotency import stable_hash
from .base import ProviderRequestError
from .gemini_wire import (
    project_current_user_content,
    project_history_contents,
    project_material_part,
    project_system_instruction,
)

GEMINI_SESSION_PROJECTION_METADATA_KEY = "_relay_gemini_projection"
GEMINI_PHYSICAL_LAYOUT_VERSION_V2 = "gemini-physical-cache-layout/2"
GEMINI_PHYSICAL_LAYOUT_VERSION_V3 = "gemini-physical-cache-layout/3"
GEMINI_PHYSICAL_LAYOUT_VERSION_V4 = "gemini-physical-cache-layout/4"
GEMINI_PHYSICAL_LAYOUT_VERSION_V5 = "gemini-physical-cache-layout/5"
GEMINI_PHYSICAL_LAYOUT_VERSION_V6 = "gemini-physical-cache-layout/6"
GEMINI_PHYSICAL_LAYOUT_VERSION_V7 = "gemini-physical-cache-layout/7"
GEMINI_PHYSICAL_LAYOUT_VERSION = "gemini-physical-cache-layout/8"
GEMINI_PHYSICAL_LAYOUT_VERSION_V9 = "gemini-physical-cache-layout/9"
SUPPORTED_GEMINI_PHYSICAL_LAYOUT_VERSIONS = {
    GEMINI_PHYSICAL_LAYOUT_VERSION_V2,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V3,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V4,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V5,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
    GEMINI_PHYSICAL_LAYOUT_VERSION,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
}
GEMINI_PHYSICAL_PROJECTOR_VERSION_V3 = "gemini-physical-projector/3"
GEMINI_PHYSICAL_PROJECTOR_VERSION_V4 = "gemini-physical-projector/4"
GEMINI_PHYSICAL_PROJECTOR_VERSION_V5 = "gemini-physical-projector/5"
GEMINI_PHYSICAL_PROJECTOR_VERSION_V6 = "gemini-physical-projector/6"
GEMINI_PHYSICAL_PROJECTOR_VERSION_V7 = "gemini-physical-projector/7"
GEMINI_PHYSICAL_PROJECTOR_VERSION = "gemini-physical-projector/8"
GEMINI_PHYSICAL_PROJECTOR_VERSION_V9 = "gemini-physical-projector/9"
PHYSICAL_PLAN_SCHEMA_VERSION_V3 = "relay-gemini-physical-cache-plan/2"
PHYSICAL_PLAN_SCHEMA_VERSION_V4 = "relay-gemini-physical-cache-plan/3"
PHYSICAL_PLAN_SCHEMA_VERSION_V5 = "relay-gemini-physical-cache-plan/4"
PHYSICAL_PLAN_SCHEMA_VERSION_V6 = "relay-gemini-physical-cache-plan/5"
PHYSICAL_PLAN_SCHEMA_VERSION_V7 = "relay-gemini-physical-cache-plan/6"
PHYSICAL_PLAN_SCHEMA_VERSION = "relay-gemini-physical-cache-plan/7"
PHYSICAL_PLAN_SCHEMA_VERSION_V9 = "relay-gemini-physical-cache-plan/8"
CACHE_SPEC_SCHEMA_VERSION_V3 = "relay-gemini-cache-spec/3"
CACHE_SPEC_SCHEMA_VERSION_V4 = "relay-gemini-cache-spec/4"
CACHE_SPEC_SCHEMA_VERSION_V5 = "relay-gemini-cache-spec/5"
CACHE_SPEC_SCHEMA_VERSION_V6 = "relay-gemini-cache-spec/6"
CACHE_SPEC_SCHEMA_VERSION_V7 = "relay-gemini-cache-spec/7"
CACHE_SPEC_SCHEMA_VERSION = "relay-gemini-cache-spec/8"
CACHE_SPEC_SCHEMA_VERSION_V9 = "relay-gemini-cache-spec/9"
GEMINI_CACHE_CREATE_MAX_REQUEST_BYTES = 100_000_000


def frozen_session_projection_metadata() -> dict[str, str]:
    """Legacy default retained for callers that explicitly require layout/8."""
    return {
        "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION,
        "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION,
    }


def frozen_session_projection_metadata_v9() -> dict[str, str]:
    """Freeze layout/9 for newly created Gemini Sessions."""
    return {
        "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
        "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION_V9,
    }


def session_projection_version(session: dict[str, Any]) -> str | None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    projection = metadata.get(GEMINI_SESSION_PROJECTION_METADATA_KEY)
    if not isinstance(projection, dict):
        return None
    value = str(projection.get("layout_version") or "").strip()
    return value or None


def uses_physical_layout_v2(session: dict[str, Any]) -> bool:
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION_V2


def uses_physical_layout_v3(session: dict[str, Any]) -> bool:
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION_V3


def uses_physical_layout_v4(session: dict[str, Any]) -> bool:
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION_V4


def uses_physical_layout_v5(session: dict[str, Any]) -> bool:
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION_V5


def uses_physical_layout_v6(session: dict[str, Any]) -> bool:
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION_V6


def uses_physical_layout_v7(session: dict[str, Any]) -> bool:
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION_V7


def uses_physical_layout_v8(session: dict[str, Any]) -> bool:
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION


def uses_physical_layout_v9(session: dict[str, Any]) -> bool:
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION_V9


def uses_request_material_cache_candidates_layout(session: dict[str, Any]) -> bool:
    return uses_physical_layout_v9(session)


def uses_inline_cache_transport_layout(session: dict[str, Any]) -> bool:
    """Return whether the frozen Session uses Relay's split inline-cache transport.

    Layout/6 was the first split-transport layout. Layout/7 is intentionally
    excluded here: 4.3.1 froze that layout with a runtime-selection defect, and
    the architecture contract requires old Sessions to retain their frozen
    behavior instead of being silently upgraded. Layout/8 is the corrected
    successor for new Sessions.
    """

    return session_projection_version(session) in {
        GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
        GEMINI_PHYSICAL_LAYOUT_VERSION,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
    }


def uses_cached_instruction_layout(session: dict[str, Any]) -> bool:
    return session_projection_version(session) in {
        GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
        GEMINI_PHYSICAL_LAYOUT_VERSION,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
    }


def uses_cache_material_projection_layout(session: dict[str, Any]) -> bool:
    return session_projection_version(session) in {
        GEMINI_PHYSICAL_LAYOUT_VERSION_V3,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V4,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V5,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
        GEMINI_PHYSICAL_LAYOUT_VERSION,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
    }


def uses_supported_physical_layout(session: dict[str, Any]) -> bool:
    return session_projection_version(session) in SUPPORTED_GEMINI_PHYSICAL_LAYOUT_VERSIONS


def build_gemini_physical_cache_plan(
    *,
    snapshot: dict[str, Any],
    session: dict[str, Any],
    history: list[dict[str, Any]],
    material_ids: list[str],
    material_bindings: list[dict[str, Any]],
    context_plan: dict[str, Any],
    cache_material_projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze Gemini's provider-facing cache layout from canonical inputs.

    The canonical Session/Material/History objects stay unchanged. This plan is
    an execution-only projection and is deliberately built after Material
    bindings are frozen so cache identity, CachedContent.create and generateContent
    all consume one deterministic physical layout. Layout/6+ keeps the stable
    prefix/dynamic suffix contract, but CachedContent is built only from Relay
    inline bytes. Gemini Files API is reserved for inference-only material in
    the >=70 MiB hybrid branch and is never referenced by caches.create().
    Layout/7 freezes the Request system instruction into CachedContent so
    cached generateContent sends only dynamic contents plus generationConfig
    and the cachedContent reference. Layout/8 preserves that wire contract,
    restores the intended split inline-cache runtime selection, and requires a
    non-empty CachedContent.contents before a stateful cache can be prepared.
    """

    layout_version = session_projection_version(session)
    if layout_version not in SUPPORTED_GEMINI_PHYSICAL_LAYOUT_VERSIONS:
        raise ProviderRequestError(
            "GEMINI_PHYSICAL_LAYOUT_UNAVAILABLE",
            "The Session is not frozen to a supported Gemini physical cache layout",
        )

    model = str(snapshot.get("model") or "").strip()
    if not model:
        raise ProviderRequestError("MODEL_REQUIRED", "Gemini physical projection requires a frozen model")

    if layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V9:
        return _build_gemini_physical_cache_plan_v9(
            snapshot=snapshot,
            session=session,
            history=history,
            material_ids=material_ids,
            material_bindings=material_bindings,
            context_plan=context_plan,
            cache_material_projection=cache_material_projection,
        )

    manifest = (
        session.get("material_manifest")
        if session.get("context_policy") == "conversation" and isinstance(session.get("material_manifest"), list)
        else []
    )
    session_material_ids = [str(x) for x in manifest if str(x)]
    session_material_set = set(session_material_ids)
    binding_by_id = {str(x.get("material_id") or ""): x for x in material_bindings}

    projection = cache_material_projection if isinstance(cache_material_projection, dict) else {}
    uses_split_cache_projection = layout_version in {
        GEMINI_PHYSICAL_LAYOUT_VERSION_V3,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V4,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V5,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
        GEMINI_PHYSICAL_LAYOUT_VERSION,
    }
    if uses_split_cache_projection:
        if projection:
            cache_material_ids = [
                str(x) for x in (projection.get("cache_material_ids") or []) if str(x)
            ]
            cache_bindings_raw = projection.get("cache_material_bindings")
            cache_binding_by_id = {
                str(x.get("material_id") or ""): x
                for x in (cache_bindings_raw if isinstance(cache_bindings_raw, list) else [])
                if isinstance(x, dict) and str(x.get("material_id") or "")
            }
        else:
            if layout_version in {
                GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
                GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
                GEMINI_PHYSICAL_LAYOUT_VERSION,
            }:
                # Layout/6+ requires an explicit Relay inline-data cache
                # projection. Never fall back to putting Gemini File references
                # into CachedContent.
                cache_material_ids = []
                cache_binding_by_id = {}
            else:
                # Historical layout/3-5 direct/unit fallback.
                cache_material_ids = [
                    material_id
                    for material_id in session_material_ids
                    if str((binding_by_id.get(material_id) or {}).get("representation") or "") == "gemini_file_uri"
                ]
                cache_binding_by_id = {
                    material_id: binding_by_id[material_id]
                    for material_id in cache_material_ids
                }
    else:
        # Frozen layout/2 keeps its original one-binding-for-both semantics.
        cache_material_ids = list(session_material_ids)
        cache_binding_by_id = dict(binding_by_id)
    cache_material_set = set(cache_material_ids)

    cached_parts: list[dict[str, Any]] = []
    cached_material_descriptors: list[dict[str, Any]] = []
    occurrence_mapping: list[dict[str, Any]] = []
    for part_index, material_id in enumerate(cache_material_ids):
        binding = cache_binding_by_id.get(material_id)
        if not binding:
            raise ProviderRequestError(
                "MATERIAL_BINDING_MISSING",
                f"Frozen Gemini binding missing for Session Material {material_id}",
            )
        part = project_material_part(binding, material_id=material_id)
        cached_parts.append(part)
        descriptor = _material_descriptor(material_id, binding, part_kind=next(iter(part.keys())))
        cached_material_descriptors.append(descriptor)
        occurrence_mapping.append(
            {
                "source": "session.material_manifest",
                "material_id": material_id,
                "placement": "cached_prefix",
                "cached_content_index": 0,
                "cached_part_index": part_index,
                "representation_digest": descriptor["representation_digest"],
            }
        )

    request_instruction = project_system_instruction(snapshot)
    session_instruction = _session_stable_instruction(session)
    cache_instruction = session_instruction
    if layout_version in {GEMINI_PHYSICAL_LAYOUT_VERSION_V7, GEMINI_PHYSICAL_LAYOUT_VERSION}:
        # AIHubMix/Google reject systemInstruction on generateContent when a
        # cachedContent reference is present. Layout/7+ therefore makes the
        # Request instruction part of the immutable CachedContent identity.
        # Request instructions are canonical and take precedence over the
        # reserved Session-level instruction slot if both ever exist.
        cache_instruction = request_instruction or session_instruction

    cached_prefix: dict[str, Any] = {}
    if cached_parts:
        cached_prefix["contents"] = [{"role": "user", "parts": cached_parts}]
    if cache_instruction is not None:
        cached_prefix["systemInstruction"] = copy.deepcopy(cache_instruction)

    dynamic_history, history_occurrences, projection_safe = _project_dynamic_history(
        history,
        session_material_ids=session_material_set,
        cached_material_ids=cache_material_set,
    )
    occurrence_mapping.extend(history_occurrences)

    current_full_user = project_current_user_content(
        snapshot,
        material_ids=material_ids,
        material_bindings=material_bindings,
    )
    request_material_ids = [mid for mid in material_ids if str(mid) not in cache_material_set]
    current_uncached_user = project_current_user_content(
        snapshot,
        material_ids=request_material_ids,
        material_bindings=material_bindings,
    )
    current_occurrences = _current_material_occurrences(
        material_ids=material_ids,
        cached_material_ids=cache_material_set,
        material_bindings=binding_by_id,
    )
    occurrence_mapping.extend(current_occurrences)

    uncached_suffix: dict[str, Any] = {
        "contents": [*dynamic_history, current_uncached_user],
    }
    if request_instruction is not None and layout_version not in {
        GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
        GEMINI_PHYSICAL_LAYOUT_VERSION,
    }:
        uncached_suffix["systemInstruction"] = request_instruction

    full_uncached_payload: dict[str, Any] = {
        "contents": [*project_history_contents(history), current_full_user],
    }
    fallback_instruction = request_instruction
    if fallback_instruction is None and layout_version in {
        GEMINI_PHYSICAL_LAYOUT_VERSION_V7,
        GEMINI_PHYSICAL_LAYOUT_VERSION,
    }:
        fallback_instruction = session_instruction
    if fallback_instruction is not None:
        full_uncached_payload["systemInstruction"] = copy.deepcopy(fallback_instruction)

    is_current_layout = layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION
    is_layout_v7 = layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V7
    is_layout_v6 = layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V6
    is_layout_v5 = layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V5
    is_layout_v4 = layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V4
    projector_version = (
        GEMINI_PHYSICAL_PROJECTOR_VERSION
        if is_current_layout
        else GEMINI_PHYSICAL_PROJECTOR_VERSION_V7
        if is_layout_v7
        else GEMINI_PHYSICAL_PROJECTOR_VERSION_V6
        if is_layout_v6
        else GEMINI_PHYSICAL_PROJECTOR_VERSION_V5
        if is_layout_v5
        else GEMINI_PHYSICAL_PROJECTOR_VERSION_V4
        if is_layout_v4
        else GEMINI_PHYSICAL_PROJECTOR_VERSION_V3
    )
    physical_plan_schema_version = (
        PHYSICAL_PLAN_SCHEMA_VERSION
        if is_current_layout
        else PHYSICAL_PLAN_SCHEMA_VERSION_V7
        if is_layout_v7
        else PHYSICAL_PLAN_SCHEMA_VERSION_V6
        if is_layout_v6
        else PHYSICAL_PLAN_SCHEMA_VERSION_V5
        if is_layout_v5
        else PHYSICAL_PLAN_SCHEMA_VERSION_V4
        if is_layout_v4
        else PHYSICAL_PLAN_SCHEMA_VERSION_V3
    )
    cache_spec_schema_version = (
        CACHE_SPEC_SCHEMA_VERSION
        if is_current_layout
        else CACHE_SPEC_SCHEMA_VERSION_V7
        if is_layout_v7
        else CACHE_SPEC_SCHEMA_VERSION_V6
        if is_layout_v6
        else CACHE_SPEC_SCHEMA_VERSION_V5
        if is_layout_v5
        else CACHE_SPEC_SCHEMA_VERSION_V4
        if is_layout_v4
        else CACHE_SPEC_SCHEMA_VERSION_V3
    )

    cached_prefix_descriptor = {
        "session_instruction": session_instruction,
        "session_materials": cached_material_descriptors,
    }
    if is_current_layout or is_layout_v7:
        cached_prefix_descriptor = {
            "system_instruction": cache_instruction,
            "session_materials": cached_material_descriptors,
        }

    cache_spec = {
        "schema_version": cache_spec_schema_version,
        "layout_version": layout_version,
        "projector_version": projector_version,
        "model": model,
        "cached_prefix_descriptor": cached_prefix_descriptor,
        "semantic_dependencies": {
            "protocol_profile_hash": snapshot.get("protocol_profile_hash"),
            "capability_contract_hash": snapshot.get("capability_contract_hash"),
            "cache_contract_hash": snapshot.get("cache_contract_hash"),
        },
    }
    content_fingerprint = stable_hash(cache_spec)
    reuse_key = stable_hash(
        {
            "layout_version": layout_version,
            "model": model,
            "cached_prefix_descriptor": cache_spec["cached_prefix_descriptor"],
        }
    )

    if is_current_layout:
        # Layout/8 requires actual cached Content. systemInstruction participates
        # in cache identity but cannot by itself justify a CachedContent.create.
        cacheable = bool(cached_prefix.get("contents")) and projection_safe
    else:
        # Frozen historical layouts retain their accepted cacheability rules.
        cacheable = bool(cached_prefix.get("contents") or cached_prefix.get("systemInstruction")) and projection_safe
    projection_summary = {
        key: value
        for key, value in projection.items()
        if key != "cache_material_bindings"
    }
    dependencies = {
        "context_plan_hash": context_plan.get("context_plan_hash"),
        "material_binding_hash": stable_hash(material_bindings),
        "cache_material_projection_hash": stable_hash(projection_summary) if projection_summary else None,
        "session_material_representation_digests": [
            x["representation_digest"] for x in cached_material_descriptors
        ],
        "cache_material_ids": list(cache_material_ids),
        "inference_only_session_material_ids": [
            material_id for material_id in session_material_ids if material_id not in cache_material_set
        ],
        "history_entries": len(history),
        "projection_safe": projection_safe,
    }
    plan = {
        "schema_version": physical_plan_schema_version,
        "layout_version": layout_version,
        "projector_version": projector_version,
        "model": model,
        "cached_prefix": cached_prefix,
        "uncached_suffix": uncached_suffix,
        "full_uncached_payload": full_uncached_payload,
        "history_entry_user_content": current_full_user,
        "current_material_occurrences": current_occurrences,
        "occurrence_mapping": occurrence_mapping,
        "dependencies": dependencies,
        "cache_spec": cache_spec,
        "cache_material_projection": projection_summary,
        "cacheable": cacheable,
        "content_fingerprint": content_fingerprint if cacheable else "",
        "reuse_key": reuse_key if cacheable else "",
        # The v2 layout has a single stable Session base rather than a growing
        # committed-history prefix. Keep the existing resource schema by using a
        # fixed physical-prefix revision.
        "prefix_version": 0,
        "compatible_prefix_fingerprints": ({"0": content_fingerprint} if cacheable else {}),
        # Layout/4 introduced Provider-create threshold measurement. Layout/5
        # keeps SDK cache creation with File references for frozen Sessions.
        # Layout/6+ keeps SDK cache creation but CachedContent contains only
        # Relay inline bytes; Gemini Files API is inference-only. Layout/7+
        # freezes systemInstruction into CachedContent. Layout/8 additionally
        # requires non-empty CachedContent.contents.
        "measurement_order": (
            "provider_create"
            if (is_current_layout or is_layout_v7 or is_layout_v6 or is_layout_v5 or is_layout_v4)
            else "before_lookup"
        ),
        "cache_create_transport": (
            "google_genai_sdk"
            if (is_current_layout or is_layout_v7 or is_layout_v6 or is_layout_v5)
            else "native_rest"
        ),
    }
    plan["cached_prefix_wire_hash"] = stable_hash(cached_prefix)
    plan["uncached_suffix_wire_hash"] = stable_hash(uncached_suffix)
    plan["full_uncached_wire_hash"] = stable_hash(full_uncached_payload)
    plan["occurrence_mapping_hash"] = stable_hash(occurrence_mapping)
    plan["physical_plan_hash"] = stable_hash(plan)
    return plan



def _build_gemini_physical_cache_plan_v9(
    *,
    snapshot: dict[str, Any],
    session: dict[str, Any],
    history: list[dict[str, Any]],
    material_ids: list[str],
    material_bindings: list[dict[str, Any]],
    context_plan: dict[str, Any],
    cache_material_projection: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build layout/9: versioned committed-history prefixes and Request candidates.

    The physical projection is execution-only. It does not change canonical
    Session/Request history, and every candidate boundary keeps complete turns
    atomic. Request-only materials are candidates only for explicit-context
    Sessions; resource lookup remains fenced by the Session cache scope.
    """
    model = str(snapshot.get("model") or "").strip()
    context_policy = str(session.get("context_policy") or "conversation")
    manifest = (
        session.get("material_manifest")
        if context_policy == "conversation" and isinstance(session.get("material_manifest"), list)
        else []
    )
    session_material_ids = [str(value) for value in manifest if str(value)]
    session_material_set = set(session_material_ids)
    binding_by_id = {
        str(value.get("material_id") or ""): value
        for value in material_bindings
        if isinstance(value, dict) and str(value.get("material_id") or "")
    }

    projection = cache_material_projection if isinstance(cache_material_projection, dict) else {}
    cache_material_ids = [
        str(value) for value in (projection.get("cache_material_ids") or []) if str(value)
    ]
    allowed_candidates = set(session_material_ids if context_policy == "conversation" else material_ids)
    if any(value not in allowed_candidates for value in cache_material_ids):
        raise ProviderRequestError(
            "GEMINI_CACHE_MATERIAL_CANDIDATE_INVALID",
            "Layout/9 cache material candidates are outside the frozen Session/Request context",
        )
    if len(cache_material_ids) != len(set(cache_material_ids)):
        raise ProviderRequestError(
            "GEMINI_CACHE_MATERIAL_CANDIDATE_INVALID",
            "Layout/9 cache material candidates contain duplicate identities",
        )
    cache_material_set = set(cache_material_ids)

    raw_cache_bindings = projection.get("cache_material_bindings")
    cache_binding_by_id = {
        str(value.get("material_id") or ""): value
        for value in (raw_cache_bindings if isinstance(raw_cache_bindings, list) else [])
        if isinstance(value, dict) and str(value.get("material_id") or "")
    }
    cached_parts: list[dict[str, Any]] = []
    cached_material_descriptors: list[dict[str, Any]] = []
    occurrence_mapping: list[dict[str, Any]] = []
    for part_index, material_id in enumerate(cache_material_ids):
        binding = cache_binding_by_id.get(material_id)
        if not binding:
            raise ProviderRequestError(
                "MATERIAL_BINDING_MISSING",
                f"Frozen Gemini inline cache binding missing for Material {material_id}",
            )
        if str(binding.get("representation") or binding.get("binding_kind") or "") != "gemini_inline_data":
            raise ProviderRequestError(
                "GEMINI_CACHE_FILE_REF_FORBIDDEN",
                "Layout/9 CachedContent accepts Relay inlineData only; Gemini File and External URL references are inference-only",
            )
        part = project_material_part(binding, material_id=material_id)
        if "inlineData" not in part:
            raise ProviderRequestError(
                "GEMINI_CACHE_FILE_REF_FORBIDDEN",
                "Layout/9 CachedContent accepts inlineData only",
            )
        cached_parts.append(part)
        descriptor = _material_descriptor(material_id, binding, part_kind="inlineData")
        cached_material_descriptors.append(descriptor)
        occurrence_mapping.append(
            {
                "source": (
                    "session.material_manifest"
                    if material_id in session_material_set
                    else "request.material_ids"
                ),
                "material_id": material_id,
                "placement": "cached_prefix",
                "cached_content_index": 0,
                "cached_part_index": part_index,
                "representation_digest": descriptor["representation_digest"],
            }
        )

    request_instruction = project_system_instruction(snapshot)
    session_instruction = _session_stable_instruction(session)
    cache_instruction = request_instruction or session_instruction
    base_prefix: dict[str, Any] = {}
    if cached_parts:
        base_prefix["contents"] = [{"role": "user", "parts": cached_parts}]
    if cache_instruction is not None:
        base_prefix["systemInstruction"] = copy.deepcopy(cache_instruction)

    if context_policy == "explicit" and history:
        raise ProviderRequestError(
            "GEMINI_EXPLICIT_HISTORY_FORBIDDEN",
            "Layout/9 explicit context must not inherit Session conversation history",
        )

    history_entries, history_occurrences, projection_safe, safe_history_prefix_version = (
        _project_dynamic_history_entries_v9(
            history,
            session_material_ids=session_material_set,
            cached_material_ids=cache_material_set,
        )
    )
    occurrence_mapping.extend(history_occurrences)
    history_version = len(history) if context_policy == "conversation" else 0
    frozen_history_version = context_plan.get("history_version")
    if context_policy == "conversation" and frozen_history_version is not None:
        try:
            if int(frozen_history_version) != history_version:
                safe_history_prefix_version = 0
        except Exception:
            safe_history_prefix_version = 0

    current_full_user = project_current_user_content(
        snapshot,
        material_ids=material_ids,
        material_bindings=material_bindings,
    )
    request_material_ids = [value for value in material_ids if str(value) not in cache_material_set]
    current_uncached_user = project_current_user_content(
        snapshot,
        material_ids=request_material_ids,
        material_bindings=material_bindings,
    )
    current_occurrences = _current_material_occurrences(
        material_ids=material_ids,
        cached_material_ids=cache_material_set,
        material_bindings=binding_by_id,
    )
    occurrence_mapping.extend(current_occurrences)

    cache_plan = snapshot.get("cache_plan") if isinstance(snapshot.get("cache_plan"), dict) else {}
    provider_payload = snapshot.get("provider_payload") if isinstance(snapshot.get("provider_payload"), dict) else {}
    tool_definitions = {
        "provider_payload.tools": provider_payload.get("tools"),
        "provider_payload.toolConfig": provider_payload.get("toolConfig"),
        "snapshot.tools": snapshot.get("tools"),
        "snapshot.tool_config": snapshot.get("tool_config"),
        "snapshot.toolConfig": snapshot.get("toolConfig"),
        "snapshot.function_declarations": snapshot.get("function_declarations"),
    }
    semantic_dependencies = {
        "protocol_profile_hash": snapshot.get("protocol_profile_hash"),
        "capability_contract_hash": snapshot.get("capability_contract_hash"),
        "cache_contract_hash": cache_plan.get("cache_contract_hash") or snapshot.get("cache_contract_hash"),
        "tool_definitions_hash": stable_hash(tool_definitions),
    }
    request_dependencies = {
        "effective_options_hash": stable_hash(snapshot.get("effective_options") or {}),
        "think_level": snapshot.get("think_level"),
        "structured_output_hash": stable_hash(snapshot.get("structured_output") or {}),
        "provider_payload_hash": stable_hash(snapshot.get("provider_payload") or {}),
    }
    unsupported_cached_fields = sorted(
        key for key, value in tool_definitions.items()
        if value not in (None, [], {})
    )
    base_descriptor = {
        "system_instruction": cache_instruction,
        "cache_materials": cached_material_descriptors,
        "semantic_dependencies": semantic_dependencies,
    }
    reuse_key = stable_hash(
        {
            "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
            "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION_V9,
            "model": model,
            "base_prefix_descriptor": base_descriptor,
        }
    )

    ttl_value = None
    mechanism_config = cache_plan.get("mechanism_config") if isinstance(cache_plan.get("mechanism_config"), dict) else {}
    if isinstance(mechanism_config.get("ttl_seconds"), int) and mechanism_config.get("ttl_seconds") > 0:
        ttl_value = int(mechanism_config["ttl_seconds"])
    ttl_value = ttl_value or 3600

    def _candidate_prefix(version: int) -> dict[str, Any]:
        contents = list(base_prefix.get("contents") or [])
        for entry_contents in history_entries[:version]:
            contents.extend(copy.deepcopy(entry_contents))
        candidate: dict[str, Any] = {}
        if contents:
            candidate["contents"] = contents
        if cache_instruction is not None:
            candidate["systemInstruction"] = copy.deepcopy(cache_instruction)
        return candidate

    def _candidate_spec(version: int, candidate_prefix: dict[str, Any]) -> dict[str, Any]:
        history_prefix_contents = [
            content
            for entry_contents in history_entries[:version]
            for content in entry_contents
        ]
        return {
            "schema_version": CACHE_SPEC_SCHEMA_VERSION_V9,
            "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
            "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION_V9,
            "model": model,
            "base_prefix_descriptor": base_descriptor,
            "history_prefix": {
                "version": version,
                "entry_count": version,
                "wire_hash": stable_hash(history_prefix_contents),
            },
            "cached_prefix_wire_hash": stable_hash(candidate_prefix),
            "semantic_dependencies": semantic_dependencies,
        }

    compatible_prefix_fingerprints: dict[str, str] = {}
    candidate_prefixes: dict[str, dict[str, Any]] = {}
    candidate_request_bytes: dict[str, int] = {}
    max_history_prefix_version = 0
    max_candidate_found = False
    candidate_size_exceeded = False
    oversized_candidate_request_bytes: int | None = None
    for version in range(safe_history_prefix_version + 1):
        candidate_prefix = _candidate_prefix(version)
        if not candidate_prefix.get("contents"):
            continue
        candidate_spec = _candidate_spec(version, candidate_prefix)
        candidate_fingerprint = stable_hash(candidate_spec)
        request_body = {
            "model": f"models/{model}",
            "ttl": f"{ttl_value}s",
            "displayName": f"relay-{candidate_fingerprint[:24]}",
            **candidate_prefix,
        }
        request_bytes = len(
            json.dumps(request_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if request_bytes > GEMINI_CACHE_CREATE_MAX_REQUEST_BYTES:
            # A larger committed-history prefix cannot become smaller. Keep the
            # longest earlier boundary that still fits the complete request cap.
            candidate_size_exceeded = True
            oversized_candidate_request_bytes = request_bytes
            break
        compatible_prefix_fingerprints[str(version)] = candidate_fingerprint
        candidate_prefixes[str(version)] = candidate_prefix
        candidate_request_bytes[str(version)] = request_bytes
        max_history_prefix_version = version
        max_candidate_found = True

    cacheable = bool(max_candidate_found and projection_safe and not unsupported_cached_fields)
    cacheability_reason = None
    if not projection_safe:
        cacheability_reason = "history_material_projection_unsafe"
    elif unsupported_cached_fields:
        cacheability_reason = "cached_content_unsupported_provider_fields"
    elif not max_candidate_found and candidate_size_exceeded:
        cacheability_reason = "serialized_request_too_large"
    elif not max_candidate_found:
        cacheability_reason = "no_cacheable_contents"

    cached_prefix = candidate_prefixes.get(str(max_history_prefix_version), {}) if max_candidate_found else {}
    content_fingerprint = compatible_prefix_fingerprints.get(str(max_history_prefix_version), "") if max_candidate_found else ""
    current_suffix = {
        "contents": [
            *[
                content
                for entry_contents in history_entries[max_history_prefix_version:]
                for content in entry_contents
            ],
            current_uncached_user,
        ]
    }
    suffixes_by_prefix_version: dict[str, dict[str, Any]] = {}
    suffix_hashes_by_prefix_version: dict[str, str] = {}
    for raw_version in compatible_prefix_fingerprints:
        version = int(raw_version)
        suffix = {
            "contents": [
                *[
                    content
                    for entry_contents in history_entries[version:]
                    for content in entry_contents
                ],
                current_uncached_user,
            ]
        }
        suffixes_by_prefix_version[raw_version] = suffix
        suffix_hashes_by_prefix_version[raw_version] = stable_hash(suffix)

    # Implicit caching has no CachedContent body-size or non-empty-contents
    # requirement. Its stable prefix is therefore computed independently from
    # the explicit-resource candidate, while retaining the same safe turn boundary.
    implicit_stable_prefix = _candidate_prefix(safe_history_prefix_version)
    implicit_suffix = {
        "contents": [
            *[
                content
                for entry_contents in history_entries[safe_history_prefix_version:]
                for content in entry_contents
            ],
            current_uncached_user,
        ]
    }
    implicit_prefix_payload = {
        "contents": [
            *(copy.deepcopy(implicit_stable_prefix.get("contents") or [])),
            *(copy.deepcopy(implicit_suffix.get("contents") or [])),
        ]
    }
    if cache_instruction is not None:
        implicit_prefix_payload["systemInstruction"] = copy.deepcopy(cache_instruction)
    implicit_prefix_measurement_payload = copy.deepcopy(implicit_stable_prefix)
    effective_tools = provider_payload.get("tools") if provider_payload.get("tools") is not None else snapshot.get("tools")
    effective_tool_config = (
        provider_payload.get("toolConfig")
        if provider_payload.get("toolConfig") is not None
        else snapshot.get("tool_config") if snapshot.get("tool_config") is not None
        else snapshot.get("toolConfig")
    )
    if effective_tools not in (None, [], {}):
        implicit_prefix_measurement_payload["tools"] = copy.deepcopy(effective_tools)
    if effective_tool_config not in (None, [], {}):
        implicit_prefix_measurement_payload["toolConfig"] = copy.deepcopy(effective_tool_config)
    implicit_prefix_eligible = bool(
        projection_safe and (implicit_stable_prefix.get("contents") or implicit_stable_prefix.get("systemInstruction"))
    )
    implicit_prefix_compatible_fingerprints = {
        str(version): stable_hash(
            {
                "schema_version": "relay-gemini-implicit-prefix/1",
                "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
                "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION_V9,
                "model": model,
                "prefix": _candidate_prefix(version),
                "semantic_dependencies": semantic_dependencies,
                "scope": {
                    "session_id": session.get("id"),
                    "connection_id": snapshot.get("connection_id"),
                    "offering_id": snapshot.get("offering_id"),
                    "account_scope_hash": ((session.get("metadata") or {}).get("_relay_route") or {}).get("account_scope_hash"),
                    "protocol_profile_hash": snapshot.get("protocol_profile_hash"),
                },
            }
        )
        for version in range(safe_history_prefix_version + 1)
    }

    full_uncached_payload: dict[str, Any] = {
        "contents": [*project_history_contents(history), current_full_user],
    }
    fallback_instruction = request_instruction or session_instruction
    if fallback_instruction is not None:
        full_uncached_payload["systemInstruction"] = copy.deepcopy(fallback_instruction)

    for history_index in range(len(history)):
        occurrence_mapping.append(
            {
                "source": "history_entry",
                "history_entry_index": history_index,
                "placement": (
                    "cached_prefix"
                    if history_index < max_history_prefix_version and cacheable
                    else "uncached_suffix"
                ),
            }
        )

    projection_summary = {
        key: value for key, value in projection.items() if key != "cache_material_bindings"
    }
    cache_spec = _candidate_spec(max_history_prefix_version, cached_prefix) if cacheable else {
        "schema_version": CACHE_SPEC_SCHEMA_VERSION_V9,
        "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
        "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION_V9,
        "model": model,
        "base_prefix_descriptor": base_descriptor,
        "semantic_dependencies": semantic_dependencies,
        "request_dependencies": request_dependencies,
    }
    plan = {
        "schema_version": PHYSICAL_PLAN_SCHEMA_VERSION_V9,
        "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
        "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION_V9,
        "model": model,
        "cached_prefix": cached_prefix,
        "uncached_suffix": suffixes_by_prefix_version.get(str(max_history_prefix_version), current_suffix),
        "uncached_suffix_by_prefix_version": suffixes_by_prefix_version,
        "uncached_suffix_wire_hashes": suffix_hashes_by_prefix_version,
        "implicit_prefix_payload": implicit_prefix_payload,
        "implicit_prefix_measurement_payload": implicit_prefix_measurement_payload,
        "implicit_prefix_eligible": implicit_prefix_eligible,
        "implicit_prefix_fingerprint": implicit_prefix_compatible_fingerprints.get(str(safe_history_prefix_version), "") if implicit_prefix_eligible else "",
        "implicit_prefix_compatible_fingerprints": implicit_prefix_compatible_fingerprints if implicit_prefix_eligible else {},
        "full_uncached_payload": full_uncached_payload,
        "history_entry_user_content": current_full_user,
        "current_material_occurrences": current_occurrences,
        "occurrence_mapping": occurrence_mapping,
        "dependencies": {
            "context_plan_hash": context_plan.get("context_plan_hash"),
            "material_binding_hash": stable_hash(material_bindings),
            "cache_material_projection_hash": stable_hash(projection_summary) if projection_summary else None,
            "cache_material_ids": list(cache_material_ids),
            "cache_candidate_source": "session.material_manifest" if context_policy == "conversation" else "request.material_ids",
            "inference_only_session_material_ids": [
                value for value in session_material_ids if value not in cache_material_set
            ],
            "inference_only_material_ids": [value for value in material_ids if value not in cache_material_set],
            "history_entries": len(history),
            "history_version": history_version,
            "history_cached_prefix_version": max_history_prefix_version if cacheable else 0,
            "safe_history_prefix_version": safe_history_prefix_version,
            "projection_safe": projection_safe,
            "cache_create_request_bytes": (
                candidate_request_bytes.get(str(max_history_prefix_version))
                if max_candidate_found
                else oversized_candidate_request_bytes
            ),
            "cache_create_oversized_candidate_request_bytes": oversized_candidate_request_bytes,
            "cache_create_max_request_bytes": GEMINI_CACHE_CREATE_MAX_REQUEST_BYTES,
            "cacheability_reason": cacheability_reason,
            "request_dependencies": request_dependencies,
        },
        "cache_spec": cache_spec,
        "cache_material_projection": projection_summary,
        "cacheable": cacheable,
        "cacheability_reason": cacheability_reason,
        "content_fingerprint": content_fingerprint if cacheable else "",
        "reuse_key": reuse_key if cacheable else "",
        "prefix_version": max_history_prefix_version if cacheable else 0,
        "compatible_prefix_fingerprints": compatible_prefix_fingerprints if cacheable else {},
        "measurement_order": "provider_create",
        "cache_create_transport": "google_genai_sdk",
    }
    plan["cached_prefix_wire_hash"] = stable_hash(cached_prefix)
    plan["uncached_suffix_wire_hash"] = stable_hash(plan["uncached_suffix"])
    plan["full_uncached_wire_hash"] = stable_hash(full_uncached_payload)
    plan["occurrence_mapping_hash"] = stable_hash(occurrence_mapping)
    plan["implicit_prefix_payload_hash"] = stable_hash(implicit_prefix_payload)
    plan["physical_plan_hash"] = stable_hash(plan)
    return plan


def _project_dynamic_history_entries_v9(
    history: list[dict[str, Any]],
    *,
    session_material_ids: set[str],
    cached_material_ids: set[str],
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]], bool, int]:
    """Project complete history turns and identify the longest safe prefix.

    Request-only attachment references are never frozen into CachedContent:
    their historical turn and all later turns stay in the uncached suffix. The
    Session-material occurrence map remains mandatory before stripping a part.
    """
    entries: list[list[dict[str, Any]]] = []
    mapping: list[dict[str, Any]] = []
    projection_safe = True
    prefix_open = True
    safe_prefix_version = 0

    for history_index, turn in enumerate(history):
        turn_contents: list[dict[str, Any]] = []
        transport = turn.get("transport_history") if isinstance(turn, dict) else None
        turn_safe = isinstance(transport, dict) and transport.get("kind") == "gemini_native"
        if not turn_safe:
            prefix_open = False
            entries.append(turn_contents)
            continue

        user_content = transport.get("user_content")
        model_content = transport.get("model_content")
        occurrences = transport.get("material_occurrences")
        if not isinstance(user_content, dict) or not isinstance(model_content, dict):
            turn_safe = False
        if isinstance(user_content, dict):
            projected_user = copy.deepcopy(user_content)
            source_parts = user_content.get("parts") if isinstance(user_content.get("parts"), list) else []
            remove_indexes: set[int] = set()
            known_indexes: set[int] = set()
            if isinstance(occurrences, list):
                for occurrence in occurrences:
                    if not isinstance(occurrence, dict):
                        projection_safe = False
                        turn_safe = False
                        continue
                    try:
                        part_index = int(occurrence.get("part_index"))
                    except Exception:
                        projection_safe = False
                        turn_safe = False
                        continue
                    if part_index < 0 or part_index >= len(source_parts):
                        projection_safe = False
                        turn_safe = False
                        continue
                    known_indexes.add(part_index)
                    material_id = str(occurrence.get("material_id") or "")
                    if not material_id:
                        projection_safe = False
                        turn_safe = False
                        continue
                    part = source_parts[part_index]
                    is_file_part = isinstance(part, dict) and any(
                        key in part for key in ("fileData", "inlineData")
                    )
                    if material_id in session_material_ids:
                        remove_indexes.add(part_index)
                        placement = "cached_prefix" if material_id in cached_material_ids else "uncached_suffix"
                    else:
                        placement = "uncached_suffix"
                        if is_file_part:
                            turn_safe = False
                    mapping.append(
                        {
                            "source": "history",
                            "history_entry_index": history_index,
                            "material_id": material_id,
                            "source_part_index": part_index,
                            "placement": placement,
                            "representation_digest": occurrence.get("representation_digest"),
                        }
                    )
            file_indexes = {
                index
                for index, part in enumerate(source_parts)
                if isinstance(part, dict) and any(key in part for key in ("fileData", "inlineData"))
            }
            if session_material_ids and not file_indexes.issubset(known_indexes):
                projection_safe = False
                turn_safe = False
            if not file_indexes.issubset(known_indexes):
                turn_safe = False
            projected_user["parts"] = [
                copy.deepcopy(part)
                for index, part in enumerate(source_parts)
                if index not in remove_indexes
            ]
            if projected_user["parts"]:
                turn_contents.append(projected_user)
        if isinstance(model_content, dict):
            turn_contents.append(copy.deepcopy(model_content))
        if prefix_open and turn_safe:
            safe_prefix_version += 1
        else:
            prefix_open = False
        entries.append(turn_contents)

    return entries, mapping, projection_safe, safe_prefix_version

def _session_stable_instruction(session: dict[str, Any]) -> dict[str, Any] | None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    projection = metadata.get(GEMINI_SESSION_PROJECTION_METADATA_KEY)
    if not isinstance(projection, dict):
        return None
    value = projection.get("session_instruction")
    if value in (None, ""):
        return None
    return {"parts": [{"text": str(value)}]}


def _material_descriptor(material_id: str, binding: dict[str, Any], *, part_kind: str) -> dict[str, Any]:
    descriptor = {
        "material_id": material_id,
        "content_sha256": binding.get("content_sha256"),
        "content_type": binding.get("content_type"),
        "representation": binding.get("representation") or binding.get("binding_kind"),
        "part_kind": part_kind,
    }
    descriptor["representation_digest"] = stable_hash(descriptor)
    return descriptor


def _current_material_occurrences(
    *,
    material_ids: list[str],
    cached_material_ids: set[str],
    material_bindings: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    suffix_part_index = 0
    for logical_part_index, raw_id in enumerate(material_ids):
        material_id = str(raw_id)
        binding = material_bindings.get(material_id)
        if binding is None:
            continue
        part = project_material_part(binding, material_id=material_id)
        descriptor = _material_descriptor(material_id, binding, part_kind=next(iter(part.keys())))
        cached = material_id in cached_material_ids
        occurrence = {
            "source": "current_input",
            "material_id": material_id,
            "source_part_index": logical_part_index,
            "part_index": logical_part_index,
            "placement": "cached_prefix" if cached else "uncached_suffix",
            "representation_digest": descriptor["representation_digest"],
        }
        if not cached:
            occurrence["uncached_part_index"] = suffix_part_index
            suffix_part_index += 1
        occurrences.append(occurrence)
    return occurrences


def _project_dynamic_history(
    history: list[dict[str, Any]],
    *,
    session_material_ids: set[str],
    cached_material_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    result: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []
    safe = True

    for history_index, turn in enumerate(history):
        transport = turn.get("transport_history") if isinstance(turn, dict) else None
        if not isinstance(transport, dict) or transport.get("kind") != "gemini_native":
            continue
        user_content = transport.get("user_content")
        model_content = transport.get("model_content")
        occurrences = transport.get("material_occurrences")

        if isinstance(user_content, dict):
            projected_user = copy.deepcopy(user_content)
            parts = projected_user.get("parts") if isinstance(projected_user.get("parts"), list) else []
            source_parts = user_content.get("parts") if isinstance(user_content.get("parts"), list) else []
            remove_indexes: set[int] = set()
            known_indexes: set[int] = set()
            if isinstance(occurrences, list):
                for occurrence in occurrences:
                    if not isinstance(occurrence, dict):
                        continue
                    try:
                        part_index = int(occurrence.get("part_index"))
                    except Exception:
                        safe = False
                        continue
                    if part_index < 0 or part_index >= len(source_parts):
                        safe = False
                        continue
                    known_indexes.add(part_index)
                    material_id = str(occurrence.get("material_id") or "")
                    if not material_id:
                        safe = False
                    if material_id in session_material_ids:
                        remove_indexes.add(part_index)
                        placement = "cached_prefix" if material_id in cached_material_ids else "uncached_suffix"
                    else:
                        placement = "uncached_suffix"
                    mapping.append(
                        {
                            "source": "history",
                            "history_entry_index": history_index,
                            "material_id": material_id,
                            "source_part_index": part_index,
                            "placement": placement,
                            "representation_digest": occurrence.get("representation_digest"),
                        }
                    )
                file_indexes = {
                    index
                    for index, part in enumerate(source_parts)
                    if isinstance(part, dict) and any(k in part for k in ("fileData", "inlineData"))
                }
                if session_material_ids and not file_indexes.issubset(known_indexes):
                    safe = False
            else:
                # A new-layout Session should only accumulate Gemini history with
                # explicit occurrence metadata. If an older/mixed entry contains
                # file parts, do not guess which ones are Session-stable.
                file_indexes = {
                    index
                    for index, part in enumerate(source_parts)
                    if isinstance(part, dict) and any(k in part for k in ("fileData", "inlineData"))
                }
                if file_indexes and session_material_ids:
                    safe = False

            projected_user["parts"] = [part for index, part in enumerate(parts) if index not in remove_indexes]
            if projected_user["parts"]:
                result.append(projected_user)
        if isinstance(model_content, dict):
            result.append(copy.deepcopy(model_content))

    return result, mapping, safe
