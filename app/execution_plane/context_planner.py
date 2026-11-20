from app.materials.classification import material_cache_class

import json
from typing import Any

from app.core.idempotency import stable_hash
from app.core.context_codec import freeze_history, normalize_native_payload


def _content_hash(value: Any) -> str:
    return stable_hash(value)


def build_context_plan(
    *,
    snapshot: dict[str, Any],
    session: dict[str, Any],
    material_rows: list[dict[str, Any]],
    history_identity: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, provider-neutral cache context IR.

    The plan describes the existing semantic order. It deliberately does not
    rewrite prompt text, roles, material MIME, tool order, or history. Provider
    adapters may only project anchors already present in this IR.
    """

    upgraded = int((snapshot.get("cache_upgrade") or {}).get("version") or 0) >= 3
    segments: list[dict[str, Any]] = []
    ordinal = 0

    instructions = snapshot.get("instructions")
    if instructions:
        segments.append(
            {
                "segment_id": f"seg-{ordinal}",
                "domain": "instructions",
                "role": "system",
                "source_ref": "request.instructions",
                "canonical_content_hash": _content_hash(str(instructions)),
                "ordinal": ordinal,
                "stability": "request_stable",
                "sensitivity": "inherit",
                "semantic_boundary": "instructions_end",
                "dependency_ids": [],
            }
        )
        ordinal += 1

    if upgraded:
        native = snapshot.get("provider_payload") or {}
        tools = native.get("tools", snapshot.get("tools"))
        if tools:
            value = normalize_native_payload({"tools": tools}, response_schemas=True, unordered_tools=(
                ((snapshot.get("protocol_profile") or {}).get("cache") or {}).get("unordered_tools") is True))
            segments.append({"segment_id": f"seg-{ordinal}", "domain": "tools", "role": "tool_schema",
                             "source_ref": "request.tools", "canonical_content_hash": stable_hash(value),
                             "ordinal": ordinal, "stability": "request_stable", "semantic_boundary": "tools_end",
                             "dependency_ids": []})
            ordinal += 1

    session_materials = {
        str(x) for x in (session.get("material_manifest") or []) if str(x)
    }
    for row in material_rows:
        material_id = str(row.get("id") or "")
        if not material_id:
            continue
        segments.append(
            {
                "segment_id": f"seg-{ordinal}",
                "domain": "material",
                **({"material_cache_class": material_cache_class(row.get("content_type"))} if upgraded else {}),
                "role": "user_material",
                "source_ref": material_id,
                "canonical_content_hash": str(row.get("sha256") or ""),
                "material_representation_digest": stable_hash(
                    {
                        "filename": row.get("filename"),
                        "content_type": row.get("content_type"),
                        "sha256": row.get("sha256"),
                    }
                ),
                "ordinal": ordinal,
                "stability": "session_stable" if material_id in session_materials else "request_stable",
                "sensitivity": "inherit",
                "semantic_boundary": "material_occurrence",
                "dependency_ids": [material_id],
            }
        )
        ordinal += 1

    if (upgraded and history is not None and session.get("context_policy") == "conversation"
            and session.get("history_mode", "cumulative") != "ephemeral_turn"):
        for turn_index, turn in enumerate(freeze_history(history)):
            segments.append({
                "segment_id": f"seg-{ordinal}", "domain": "history", "role": "conversation_turn",
                "source_ref": f"history_turn:{turn_index}",
                "canonical_content_hash": stable_hash(turn.get("transport_history") or {}),
                "ordinal": ordinal, "stability": "committed_history",
                "semantic_boundary": "history_turn_end", "dependency_ids": [f"history_turn:{turn_index}"],
            })
            ordinal += 1
    elif (session.get("context_policy") == "conversation"
            and session.get("history_mode", "cumulative") != "ephemeral_turn" and history_identity):
        segments.append(
            {
                "segment_id": f"seg-{ordinal}",
                "domain": "history",
                "role": "conversation_history",
                "source_ref": history_identity.get("object_id"),
                "canonical_content_hash": history_identity.get("sha256") or stable_hash(history_identity),
                "ordinal": ordinal,
                "stability": "committed_history",
                "sensitivity": "inherit",
                "semantic_boundary": "history_prefix_end",
                "dependency_ids": [
                    f"history_version:{int(session.get('history_version') or 0)}"
                ],
            }
        )
        ordinal += 1

    input_value = snapshot.get("input")
    segments.append(
        {
            "segment_id": f"seg-{ordinal}",
            "domain": "input",
            "role": "user",
            "source_ref": "request.input",
            "canonical_content_hash": _content_hash(input_value),
            "ordinal": ordinal,
            "stability": "dynamic_suffix",
            "sensitivity": "inherit",
            "semantic_boundary": "request_suffix",
            "dependency_ids": [],
        }
    )

    stable_prefix = [x for x in segments if x.get("stability") != "dynamic_suffix"]
    plan = {
        "schema_version": "relay-context-plan/1",
        "history_version": int(session.get("history_version") or 0),
        "history_object_id": session.get("history_object_id"),
        "segments": segments,
        "stable_prefix_segment_ids": [x["segment_id"] for x in stable_prefix],
        "stable_prefix_fingerprint": stable_hash(
            [
                {
                    "role": x.get("role"),
                    "domain": x.get("domain"),
                    "hash": x.get("canonical_content_hash"),
                    "representation": x.get("material_representation_digest"),
                    "ordinal": x.get("ordinal"),
                }
                for x in stable_prefix
            ]
        ),
        # Token measurement is intentionally separate. Do not pretend byte or
        # character counts are exact provider token counts.
        "token_assessment": {
            "status": "unmeasured",
            "lower": None,
            "upper": None,
            "source": None,
            "estimator_version": None,
        },
    }
    materials = [
        {"content_sha256": x.get("canonical_content_hash"),
         "representation_digest": x.get("material_representation_digest")}
        for x in segments if x.get("domain") == "material"
    ]
    plan["material_reuse"] = {
        "schema_version": "relay-material-reuse/1",
        "ordered_material_fingerprint": stable_hash(materials) if materials else None,
        "material_count": len(materials),
        "identity_complete": bool(materials) and all(x["content_sha256"] for x in materials),
        "scope": "session", "projection_reuse_enabled": True,
    }
    if isinstance(snapshot.get("cache_upgrade"), dict):
        plan["cache_upgrade"] = snapshot["cache_upgrade"]
        if int(snapshot["cache_upgrade"].get("version") or 0) >= 2:
            plan["session_context"] = {
                "history_mode": snapshot.get("history_mode", "cumulative"),
                "sticky_scope": snapshot.get("sticky_scope", "session"),
                "supply_group_id": snapshot.get("supply_group_id"),
            }
        plan["semantic_fingerprint"] = stable_hash({
            "segments": [{k: v for k, v in segment.items() if k != "source_ref"} for segment in segments],
            "materials": [segment["source_ref"] for segment in segments if segment["domain"] == "material"],
            "structured_output": snapshot.get("structured_output"),
            "options": snapshot.get("effective_options"),
            "think_level": snapshot.get("think_level"),
        })
        if upgraded or "thinking" in snapshot:
            # Logical identity remains in request_identity. Context identity has
            # no executor IDs, object IDs, timestamps, sampling, or wire handles.
            plan["semantic_fingerprint"] = stable_hash({
                "format": "relay-semantic-context/2",
                "segments": [{**{k: segment.get(k) for k in (
                    "domain", "role", "canonical_content_hash", "material_representation_digest",
                    "material_cache_class", "ordinal",
                )}, **({"material_id": segment["source_ref"]} if segment["domain"] == "material" else {})}
                    for segment in segments],
                "structured_output": snapshot.get("structured_output"),
            })
    placement = snapshot.get("material_placement_plan")
    if placement is not None:
        plan["material_placement"] = {
            "contract_version": placement["contract_version"],
            "plan_digest": placement["plan_digest"],
            "historical_material_occurrences_digest": stable_hash(placement["verified_history_occurrences"]),
            "current_turn_material_ids": placement["current_effective_material_ids"],
        }
    plan["context_plan_hash"] = stable_hash(plan)
    return plan


def context_plan_preview(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))



class ContextPlanner:
    build = staticmethod(build_context_plan)
