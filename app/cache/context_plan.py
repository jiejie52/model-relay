from __future__ import annotations

import json
from typing import Any

from ..core.idempotency import stable_hash


def _content_hash(value: Any) -> str:
    return stable_hash(value)


def build_context_plan(
    *,
    snapshot: dict[str, Any],
    session: dict[str, Any],
    material_rows: list[dict[str, Any]],
    history_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, provider-neutral cache context IR.

    The plan describes the existing semantic order. It deliberately does not
    rewrite prompt text, roles, material MIME, tool order, or history. Provider
    adapters may only project anchors already present in this IR.
    """

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

    if session.get("context_policy") == "conversation" and history_identity:
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
    plan["context_plan_hash"] = stable_hash(plan)
    return plan


def context_plan_preview(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
