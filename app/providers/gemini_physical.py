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
GEMINI_PHYSICAL_LAYOUT_VERSION = "gemini-physical-cache-layout/2"
GEMINI_PHYSICAL_PROJECTOR_VERSION = "gemini-physical-projector/2"
PHYSICAL_PLAN_SCHEMA_VERSION = "relay-gemini-physical-cache-plan/1"
CACHE_SPEC_SCHEMA_VERSION = "relay-gemini-cache-spec/2"


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
    return session_projection_version(session) == GEMINI_PHYSICAL_LAYOUT_VERSION


def build_gemini_physical_cache_plan(
    *,
    snapshot: dict[str, Any],
    session: dict[str, Any],
    history: list[dict[str, Any]],
    material_ids: list[str],
    material_bindings: list[dict[str, Any]],
    context_plan: dict[str, Any],
) -> dict[str, Any]:
    """Freeze Gemini's provider-facing cache layout from canonical inputs.

    The canonical Session/Material/History objects stay unchanged. This plan is
    an execution-only projection and is deliberately built after Material
    bindings are frozen so countTokens, cache identity, CachedContent.create and
    generateContent all consume one deterministic physical layout.
    """

    layout_version = session_projection_version(session)
    if layout_version != GEMINI_PHYSICAL_LAYOUT_VERSION:
        raise ProviderRequestError(
            "GEMINI_PHYSICAL_LAYOUT_UNAVAILABLE",
            "The Session is not frozen to the Gemini physical cache layout required by this projector",
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

    cached_parts: list[dict[str, Any]] = []
    cached_material_descriptors: list[dict[str, Any]] = []
    occurrence_mapping: list[dict[str, Any]] = []
    for part_index, material_id in enumerate(session_material_ids):
        binding = binding_by_id.get(material_id)
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

    cached_prefix: dict[str, Any] = {}
    if cached_parts:
        cached_prefix["contents"] = [{"role": "user", "parts": cached_parts}]

    # There is currently no public canonical Session-level instruction field.
    # Request/stage instructions therefore remain in the uncached suffix. The
    # schema leaves this slot explicit so a future canonical Session instruction
    # can be added without changing the physical-plan contract.
    session_instruction = _session_stable_instruction(session)
    if session_instruction is not None:
        cached_prefix["systemInstruction"] = session_instruction

    dynamic_history, history_occurrences, projection_safe = _project_dynamic_history(
        history,
        session_material_ids=session_material_set,
    )
    occurrence_mapping.extend(history_occurrences)

    current_full_user = project_current_user_content(
        snapshot,
        material_ids=material_ids,
        material_bindings=material_bindings,
    )
    request_material_ids = [mid for mid in material_ids if str(mid) not in session_material_set]
    current_uncached_user = project_current_user_content(
        snapshot,
        material_ids=request_material_ids,
        material_bindings=material_bindings,
    )
    current_occurrences = _current_material_occurrences(
        material_ids=material_ids,
        session_material_ids=session_material_set,
        material_bindings=binding_by_id,
    )
    occurrence_mapping.extend(current_occurrences)

    uncached_suffix: dict[str, Any] = {
        "contents": [*dynamic_history, current_uncached_user],
    }
    request_instruction = project_system_instruction(snapshot)
    if request_instruction is not None:
        uncached_suffix["systemInstruction"] = request_instruction

    full_uncached_payload: dict[str, Any] = {
        "contents": [*project_history_contents(history), current_full_user],
    }
    if request_instruction is not None:
        full_uncached_payload["systemInstruction"] = copy.deepcopy(request_instruction)

    cache_spec = {
        "schema_version": CACHE_SPEC_SCHEMA_VERSION,
        "layout_version": layout_version,
        "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION,
        "model": model,
        "cached_prefix_descriptor": {
            "session_instruction": session_instruction,
            "session_materials": cached_material_descriptors,
        },
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
    dependencies = {
        "context_plan_hash": context_plan.get("context_plan_hash"),
        "material_binding_hash": stable_hash(material_bindings),
        "session_material_representation_digests": [
            x["representation_digest"] for x in cached_material_descriptors
        ],
        "history_entries": len(history),
        "projection_safe": projection_safe,
    }
    plan = {
        "schema_version": PHYSICAL_PLAN_SCHEMA_VERSION,
        "layout_version": layout_version,
        "projector_version": GEMINI_PHYSICAL_PROJECTOR_VERSION,
        "model": model,
        "cached_prefix": cached_prefix,
        "uncached_suffix": uncached_suffix,
        "full_uncached_payload": full_uncached_payload,
        "history_entry_user_content": current_full_user,
        "current_material_occurrences": current_occurrences,
        "occurrence_mapping": occurrence_mapping,
        "dependencies": dependencies,
        "cache_spec": cache_spec,
        "cacheable": cacheable,
        "content_fingerprint": content_fingerprint if cacheable else "",
        "reuse_key": reuse_key if cacheable else "",
        # The v2 layout has a single stable Session base rather than a growing
        # committed-history prefix. Keep the existing resource schema by using a
        # fixed physical-prefix revision.
        "prefix_version": 0,
        "compatible_prefix_fingerprints": ({"0": content_fingerprint} if cacheable else {}),
        "measurement_order": "before_lookup",
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
    session_material_ids: set[str],
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
        cached = material_id in session_material_ids
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
                        placement = "cached_prefix"
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
