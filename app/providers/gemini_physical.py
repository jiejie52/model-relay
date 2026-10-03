from __future__ import annotations

import copy
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
GEMINI_PHYSICAL_LAYOUT_VERSION = "gemini-physical-cache-layout/7"
SUPPORTED_GEMINI_PHYSICAL_LAYOUT_VERSIONS = {
    GEMINI_PHYSICAL_LAYOUT_VERSION_V2,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V3,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V4,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V5,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
    GEMINI_PHYSICAL_LAYOUT_VERSION,
}
GEMINI_PHYSICAL_PROJECTOR_VERSION_V3 = "gemini-physical-projector/3"
GEMINI_PHYSICAL_PROJECTOR_VERSION_V4 = "gemini-physical-projector/4"
GEMINI_PHYSICAL_PROJECTOR_VERSION_V5 = "gemini-physical-projector/5"
GEMINI_PHYSICAL_PROJECTOR_VERSION_V6 = "gemini-physical-projector/6"
GEMINI_PHYSICAL_PROJECTOR_VERSION = "gemini-physical-projector/7"
PHYSICAL_PLAN_SCHEMA_VERSION_V3 = "relay-gemini-physical-cache-plan/2"
PHYSICAL_PLAN_SCHEMA_VERSION_V4 = "relay-gemini-physical-cache-plan/3"
PHYSICAL_PLAN_SCHEMA_VERSION_V5 = "relay-gemini-physical-cache-plan/4"
PHYSICAL_PLAN_SCHEMA_VERSION_V6 = "relay-gemini-physical-cache-plan/5"
PHYSICAL_PLAN_SCHEMA_VERSION = "relay-gemini-physical-cache-plan/6"
CACHE_SPEC_SCHEMA_VERSION_V3 = "relay-gemini-cache-spec/3"
CACHE_SPEC_SCHEMA_VERSION_V4 = "relay-gemini-cache-spec/4"
CACHE_SPEC_SCHEMA_VERSION_V5 = "relay-gemini-cache-spec/5"
CACHE_SPEC_SCHEMA_VERSION_V6 = "relay-gemini-cache-spec/6"
CACHE_SPEC_SCHEMA_VERSION = "relay-gemini-cache-spec/7"


def frozen_session_projection_metadata() -> dict[str, str]:
    return {
        "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION,
        "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION,
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
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION


def uses_cache_material_projection_layout(session: dict[str, Any]) -> bool:
    return session_projection_version(session) in {
        GEMINI_PHYSICAL_LAYOUT_VERSION_V3,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V4,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V5,
        GEMINI_PHYSICAL_LAYOUT_VERSION_V6,
        GEMINI_PHYSICAL_LAYOUT_VERSION,
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
    Layout/7 additionally freezes the Request system instruction into
    CachedContent so cached generateContent sends only dynamic contents plus
    generationConfig and the cachedContent reference.
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
            if layout_version in {GEMINI_PHYSICAL_LAYOUT_VERSION_V6, GEMINI_PHYSICAL_LAYOUT_VERSION}:
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
    if layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION:
        # AIHubMix/Google reject systemInstruction on generateContent when a
        # cachedContent reference is present. Layout/7 therefore makes the
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
    if request_instruction is not None and layout_version != GEMINI_PHYSICAL_LAYOUT_VERSION:
        uncached_suffix["systemInstruction"] = request_instruction

    full_uncached_payload: dict[str, Any] = {
        "contents": [*project_history_contents(history), current_full_user],
    }
    fallback_instruction = request_instruction
    if fallback_instruction is None and layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION:
        fallback_instruction = session_instruction
    if fallback_instruction is not None:
        full_uncached_payload["systemInstruction"] = copy.deepcopy(fallback_instruction)

    is_current_layout = layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION
    is_layout_v6 = layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V6
    is_layout_v5 = layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V5
    is_layout_v4 = layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V4
    projector_version = (
        GEMINI_PHYSICAL_PROJECTOR_VERSION
        if is_current_layout
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
    if is_current_layout:
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
        # Relay inline bytes; Gemini Files API is inference-only. Layout/7 also
        # freezes systemInstruction into CachedContent.
        "measurement_order": (
            "provider_create" if (is_current_layout or is_layout_v6 or is_layout_v5 or is_layout_v4) else "before_lookup"
        ),
        "cache_create_transport": (
            "google_genai_sdk" if (is_current_layout or is_layout_v6 or is_layout_v5) else "native_rest"
        ),
    }
    plan["cached_prefix_wire_hash"] = stable_hash(cached_prefix)
    plan["uncached_suffix_wire_hash"] = stable_hash(uncached_suffix)
    plan["full_uncached_wire_hash"] = stable_hash(full_uncached_payload)
    plan["occurrence_mapping_hash"] = stable_hash(occurrence_mapping)
    plan["physical_plan_hash"] = stable_hash(plan)
    return plan


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
