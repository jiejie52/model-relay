"""Stable semantic fingerprints and prefix chains for continuation anchoring."""
from __future__ import annotations

from typing import Any

from app.core.idempotency import canonical_json_bytes, stable_hash

CONTINUATION_CHAIN_FORMAT = "relay-continuation-chain/1"
LEGACY_CONTINUATION_HEAD = "relay-continuation-head/2"
INTERACTIONS_HISTORY_HEAD = "relay-continuation-head/3"
CONTRACT_DRIVEN_HEAD = "relay-continuation-head/4"


def extract_turn_semantics(turn: dict[str, Any]) -> dict[str, Any]:
    """Extract canonical semantic content and material summary from a history turn.

    Excludes volatile identifiers (request_id, session_id, timestamps, etc.).
    Preserves all user/model content and material references across all supported protocols.
    """
    if not isinstance(turn, dict):
        return {"raw": str(turn)}

    content_blocks: dict[str, Any] = {}
    transport = turn.get("transport_history")
    if isinstance(transport, dict):
        for field in (
            "user_content",
            "model_content",
            "user_message",
            "assistant_message",
            "user_item",
            "response_output",
        ):
            if field in transport and transport[field] is not None:
                content_blocks[field] = transport[field]

    if not content_blocks:
        for field in (
            "user_content",
            "model_content",
            "user_message",
            "assistant_message",
            "user_item",
            "response_output",
            "user",
            "assistant",
            "input",
            "output",
        ):
            if field in turn and turn[field] is not None:
                content_blocks[field] = turn[field]
        if not content_blocks and "role" in turn and "content" in turn:
            role = str(turn.get("role") or "").lower()
            if role in ("user", "human"):
                content_blocks["user_content"] = turn.get("content")
            elif role in ("assistant", "model", "system"):
                content_blocks["model_content"] = turn.get("content")
            else:
                content_blocks[f"{role}_content"] = turn.get("content")

    materials_summary: list[dict[str, Any]] = []
    if turn.get("material_occurrences"):
        for occ in turn["material_occurrences"]:
            if isinstance(occ, dict):
                materials_summary.append({
                    "material_id": occ.get("material_id"),
                    "content_sha256": occ.get("content_sha256"),
                    "part_index": occ.get("part_index"),
                })
    elif turn.get("material_ids"):
        materials_summary = [{"material_id": mid} for mid in turn["material_ids"]]

    return {
        "content": content_blocks,
        "materials": materials_summary,
    }


def build_turn_prefix_chain(history: list[dict[str, Any]], system_instructions: str | None = None) -> list[str]:
    """Calculate prefix fingerprint chain for each turn in history.

    For a history of length N, returns a list of length N where the i-th entry
    represents the deterministic semantic prefix fingerprint covering turns 0..i.
    Used for fast determination of upstream anchor coverage cutoff position.
    """
    sys_inst = system_instructions.strip() if isinstance(system_instructions, str) and system_instructions.strip() else None
    current_hash = stable_hash({
        "format": CONTINUATION_CHAIN_FORMAT,
        "system_instructions": sys_inst,
    })

    chain: list[str] = []
    for turn in history:
        turn_semantics = extract_turn_semantics(turn)
        turn_digest = stable_hash(turn_semantics)
        current_hash = stable_hash({
            "parent_prefix_hash": current_hash,
            "turn_digest": turn_digest,
        })
        chain.append(current_hash)

    return chain


def build_parent_head_fingerprint(history: list[dict[str, Any]], system_instructions: str | None = None) -> str:
    """Calculate deterministic parent session head semantic fingerprint.

    Based on visible history sequence (all valid turns' user/model content and material digests)
    and optional system instructions.
    """
    sys_inst = system_instructions.strip() if isinstance(system_instructions, str) and system_instructions.strip() else None
    if not history:
        return stable_hash({
            "format": CONTINUATION_CHAIN_FORMAT,
            "system_instructions": sys_inst,
            "turn_count": 0,
        })
    chain = build_turn_prefix_chain(history, system_instructions=sys_inst)
    return chain[-1]


def verify_anchor_fingerprint(anchor_record: dict[str, Any] | Any, current_fingerprint: str) -> bool:
    """Verify if the candidate anchor record's fingerprint matches the current fingerprint."""
    if not anchor_record or not current_fingerprint:
        return False
    if isinstance(anchor_record, dict):
        anchor_fp = anchor_record.get("fingerprint")
    else:
        anchor_fp = getattr(anchor_record, "fingerprint", None)
    if not anchor_fp or not isinstance(anchor_fp, str):
        return False
    if isinstance(current_fingerprint, (list, tuple, set)):
        return anchor_fp in current_fingerprint
    return anchor_fp == current_fingerprint


def continuation_head(
    snapshot: dict[str, Any], *, head_format: str = CONTRACT_DRIVEN_HEAD,
    head_invariants: tuple[str, ...] | None = None,
) -> str:
    """Identify the history namespace used by a frozen continuation plan."""
    scope_keys = (
        "session_id", "tenant_id", "conversation_hash", "connection_id",
        "account_scope_id", "connection_spec_hash", "protocol_profile_hash",
        "model", "upstream_model", "protocol",
    )
    if head_format == LEGACY_CONTINUATION_HEAD:
        scope_keys += ("instructions", "tools")
        return stable_hash({"format": head_format, **{key: snapshot.get(key) for key in scope_keys}})
    elif head_format == INTERACTIONS_HISTORY_HEAD:
        if snapshot.get("protocol") != "gemini_interactions":
            raise ValueError("Unsupported continuation head format for this protocol")
        return stable_hash({"format": head_format, **{key: snapshot.get(key) for key in scope_keys}})
    elif head_format == CONTRACT_DRIVEN_HEAD:
        if head_invariants is None:
            contract = (snapshot.get("capability_contract") or {}).get("continuation") or {}
            head_invariants = tuple(contract.get("head_invariants") or ("instructions", "tools", "account_scope_id", "model"))
        payload: dict[str, Any] = {"format": head_format, **{key: snapshot.get(key) for key in scope_keys}}
        if "instructions" in head_invariants:
            inst = snapshot.get("instructions")
            payload["instructions"] = inst.strip() if isinstance(inst, str) else inst
        if "tools" in head_invariants:
            from app.core.context_codec import normalize_native_payload
            raw_tools = snapshot.get("tools") or (snapshot.get("provider_payload") or {}).get("tools")
            norm_tools = normalize_native_payload({"tools": raw_tools}, response_schemas=True).get("tools") if raw_tools else None
            payload["tools"] = norm_tools
        return stable_hash(payload)
    else:
        raise ValueError(f"Unsupported continuation head format: {head_format!r}")


def extend_native_prefix(previous: str, turn: dict[str, Any]) -> str:
    transport = turn.get("transport_history") or {}
    return stable_hash({
        "previous": previous,
        "transport": {k: v for k, v in transport.items()
                      if k not in ("interaction_id", "previous_interaction_id", "upstream_handle")},
        "materials": turn.get("material_occurrences") or [],
    })


def native_prefix_chain(head: str, history: list[dict[str, Any]]) -> list[str]:
    chain = []
    for turn in history:
        head = extend_native_prefix(head, turn)
        chain.append(head)
    return chain


def continuation_material_references(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record ordered material occurrences and the resource identity required by each."""
    return [{
        "history_entry_index": occ["history_entry_index"],
        "part_index": occ["part_index"],
        "part_digest": occ["part_digest"],
        "material_id": occ["material_id"],
        "binding_generation": occ["binding_generation"],
        "representation": occ["representation_kind"],
        "external_uri": occ["binding"].get("external_uri"),
        "provider_binding_id": occ["binding"].get("provider_binding_id"),
    } for occ in occurrences]
