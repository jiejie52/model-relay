"""Provider-neutral contiguous-prefix planning. Never reorders or omits input."""
from __future__ import annotations

from typing import Any
from ..core.idempotency import canonical_json_bytes, stable_hash
from ..core.cache_fingerprint import physical_chain_v2, compute_lcp_diff


def semantic_segments(snapshot: dict[str, Any], session: dict[str, Any],
                      materials: list[dict[str, Any]], history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    def add(kind: str, value: Any, source: str, stable: bool = True):
        nodes.append({"index": len(nodes) + 1, "seg_type": kind, "semantic_hash": stable_hash(value),
                      "source_ref": source, "stable": stable,
                      "canonical_byte_size": len(canonical_json_bytes(value))})
    if snapshot.get("instructions"):
        add("sys_instr", {"role": "system", "text": snapshot["instructions"]}, "request.instructions")
    options = snapshot.get("effective_options") or {}
    schema = snapshot.get("structured_output") or {}
    if options.get("tools") or schema:
        add("tool_schema", {"tools": options.get("tools"), "structured_output": schema}, "request.schema")
    stable_order = list(dict.fromkeys(str(x) for x in session.get("material_manifest") or []))
    stable_ids = set(stable_order)
    by_id = {str(row.get("id")): row for row in materials}
    # Canonical metadata follows the frozen manifest, not DB retrieval order.
    ordered_materials = [by_id[mid] for mid in stable_order if mid in by_id]
    ordered_materials.extend(row for row in materials if str(row.get("id")) not in stable_ids)
    temporary = []
    for row in ordered_materials:
        value = {k: row.get(k) for k in ("id", "sha256", "content_type", "filename")}
        if str(row.get("id")) not in stable_ids:
            temporary.append(value)
            continue
        mime = str(row.get("content_type") or "")
        kind = ("material_docx_proj" if "wordprocessingml" in mime else "material_pdf"
                if mime == "application/pdf" else "material_video" if mime.startswith("video/") else "material_other")
        add(kind, value, str(row["id"]))
    for i, turn in enumerate(history):
        native = turn.get("transport_history") if isinstance(turn, dict) else None
        add("history_turn", native if native is not None else turn, f"history:{i + 1}")
    add("dynamic_suffix", {"input": snapshot.get("input"), "materials": temporary}, "request.input", False)
    parent = stable_hash({"format": "relay-semantic-chain/2"})
    for node in nodes:
        parent = stable_hash([parent, node["seg_type"], node["semantic_hash"]])
        node["semantic_prefix_hash"] = parent
    return nodes


class ImplicitPrefixPlanner:
    def freeze(self, segments: list[dict[str, Any]]) -> dict[str, Any]:
        # These are legal logical candidates, not a Provider hit prediction.
        stable = []
        for node in segments:
            if not node["stable"]:
                break
            stable.append(node["index"])
        return {"version": 2, "semantic_segments": segments,
                "candidate_prefix_ends": stable, "maximum_stable_segment": stable[-1] if stable else 0,
                "semantic_fingerprint": stable_hash([(x["seg_type"], x["semantic_hash"]) for x in segments])}

    def prepare(self, payload: dict[str, Any], *, scope: str, previous: list[dict[str, Any]],
                material_bindings: list[dict[str, Any]], history: list[dict[str, Any]],
                candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        chain = physical_chain_v2(payload, scope=scope, material_bindings=material_bindings, history=history)
        references = [{"physical_chain": previous}, *(candidates or [])]
        end, compared, diff = 0, None, compute_lcp_diff(previous, chain)
        for reference in references:
            if reference.get("scope", scope) != scope:
                continue
            candidate_diff = compute_lcp_diff(reference.get("physical_chain") or [], chain)
            boundary = candidate_diff["lcp_index"]
            # Whole committed turns are atomic cache anchors.
            while boundary and not chain[boundary - 1].get("safe_boundary", True):
                boundary -= 1
            if boundary > end:
                end, compared, diff = boundary, reference.get("request_id"), candidate_diff
        return {"version": 2, "physical_chain": chain, "prefix_diff": diff,
                "scope": scope, "compared_request_id": compared,
                "reusable_prefix_end": end,
                "reusable_prefix_hash": chain[end - 1]["chained_prefix_hash"] if end else None,
                "reusable_estimated_tokens": chain[end - 1]["cumulative_estimated_tokens"] if end else 0,
                "reason": "exact_contiguous_prefix" if end else "cold_or_changed_prefix",
                "provider_hit_guaranteed": False}


def prefix_summary(plan: dict[str, Any]) -> dict[str, Any]:
    return {**{k: v for k, v in plan.items() if k != "physical_chain"},
            "physical_chain_hash": stable_hash(plan.get("physical_chain") or []),
            "evidence_window": "latest_32_completed_same_session_requests"}


def legacy_grok_binding(snapshot: dict[str, Any], context_plan: dict[str, Any],
                        material_bindings: list[dict[str, Any]], session: dict[str, Any]):
    """Bypass upgraded orchestration only; retain admission and atomic dispatch."""
    from .contracts import CacheDecisionError, CacheExecutionBinding
    from .prefix import prepare_implicit_prefix_binding
    plan = snapshot.get("cache_plan") or {}
    mechanism = plan.get("planned_mechanism")
    cfg = plan.get("mechanism_config") or {}
    grok = str(snapshot.get("provider") or "").lower() in {"grok", "xai"} or str(snapshot.get("model") or "").lower().startswith("grok-")
    if not grok or snapshot.get("protocol") != "responses" or mechanism not in {None, "implicit_prefix"} or (mechanism and (
        cfg.get("threshold_mode") != "not_applicable" or plan.get("resolution_status") != "finalized"
    )):
        raise CacheDecisionError("CACHE_LEGACY_GROK_CONTRACT_INVALID", "Legacy Grok bypass requires its frozen legacy prefix contract")
    return CacheExecutionBinding(
        mechanism=mechanism, decision_reason="legacy_grok_bypass" if mechanism else str(plan.get("decision_reason") or "legacy_grok_disabled"),
        context_plan_hash=context_plan["context_plan_hash"], plan_hash=snapshot["cache_plan_hash"],
        prefix=prepare_implicit_prefix_binding(plan=plan, context_plan=context_plan, session=session) if mechanism else {},
        material_binding_hash=stable_hash(material_bindings),
        metadata={"legacy_grok_bypass": True}, projection_version="relay-grok-legacy/1")
