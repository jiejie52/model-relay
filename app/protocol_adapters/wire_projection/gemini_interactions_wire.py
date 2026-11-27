"""Gemini Interactions API Wire Projection.

Implements wire format projection for Google's POST /v1beta/interactions endpoint:
- Injects 'previous_interaction_id' and 'store': True when continuation is active.
- For immediate continuation (k == N), emits only current turn inputs and materials,
  completely stripping covered historical model outputs.
- For multi-turn delta compensation (0 <= k < N), replays history[k:N] turns preserving
  thought steps, thought signatures, and tool signatures faithfully.
- Projects interaction-scoped system_instruction, tools, and generation_config.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from app.canonical_schema import (
    StructuredOutputSpec,
    append_structured_output_prompt,
    format_structured_output_prompt_guidance,
    resolve_structured_output,
)
from app.core.context_codec import canonical_structure
from app.observability.http_wire_logger import normalization_active
from app.protocol_adapters.base import ProviderRequestError
from app.protocol_adapters.thinking_projection import project_thinking
from app.protocol_adapters.wire_projection.continuation_wire import slice_continuation_history
from app.protocol_adapters.wire_projection.gemini_wire import (
    project_current_user_content,
    project_material_part,
    project_system_instruction,
)
from app.runtime.contracts import SealedDispatchBundle


def project_gemini_interactions_payload(
    context: SealedDispatchBundle,
    connection: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project SealedDispatchBundle into Google Gemini Interactions API wire payload.

    Returns:
        (payload, user_content) where payload is the JSON-serializable request body
        for POST /v1beta/interactions, and user_content is the native turn representation.
    """
    snapshot = context.snapshot
    session = context.session
    history = context.history or []

    # 1. Project current user content and material attachments
    user_content = project_current_user_content(
        snapshot,
        material_ids=context.material_ids or [],
        material_bindings=context.material_bindings or [],
    )

    # 2. Resolve model name
    model = str(snapshot.get("upstream_model") or snapshot.get("model") or "gemini-2.5-pro")
    if not model.startswith("models/"):
        model_name = f"models/{model}"
    else:
        model_name = model

    # 3. Read continuation binding
    binding = context.continuation_execution_binding or {}
    if hasattr(binding, "to_dict"):
        binding = binding.to_dict()
    elif not isinstance(binding, dict):
        binding = {}

    fallback = bool(binding.get("fallback_occurred", False))
    upstream_handle = binding.get("upstream_handle") or binding.get("upstream_anchor_ref")
    mechanism = str(binding.get("mechanism") or "none").lower()
    effective_mode = str(binding.get("effective_mode") or "").lower()

    continuation_active = (
        not fallback
        and upstream_handle is not None
        and str(upstream_handle).strip() != ""
        and mechanism not in ("none", "off")
        and effective_mode not in ("disabled", "stateless_full_replay")
    )

    versioned_proof = binding.get("schema_version") == "relay-continuation-binding/2"

    # 4. Assemble input turns based on continuation state
    payload_input: list[dict[str, Any]] | str
    previous_interaction_id: str | None = None
    store: bool = False

    req_policy = (
        snapshot.get("request_context_policy")
        or snapshot.get("context_policy")
        or session.get("context_policy")
        or "conversation"
    )
    is_non_cumulative = (
        req_policy in ("explicit", "session_materials_only")
        or session.get("history_mode") == "ephemeral_turn"
    )

    if continuation_active and not is_non_cumulative:
        previous_interaction_id = str(upstream_handle)
        store = True if binding.get("store_upstream_state") is not False else False

        # Determine delta turns
        bound_delta = binding.get("bound_delta_turns")
        if versioned_proof:
            delta_turns = slice_continuation_history(history, binding["covered_turn_count"], allow_fallback=False)
        elif bound_delta is not None and isinstance(bound_delta, list):
            delta_turns = bound_delta
        else:
            cov_count = binding.get("covered_turn_count") or binding.get("delta_start_turn_index")
            try:
                cov_int = int(cov_count) if cov_count is not None else len(history)
            except (ValueError, TypeError):
                cov_int = len(history)
            delta_turns = slice_continuation_history(history, cov_int, allow_fallback=True)

        if not delta_turns:
            # k == N: Immediate continuation!
            # The payload's input contains ONLY current turn input, stripping all prior model outputs.
            payload_input = _format_current_turn_input(user_content)
        else:
            # 0 <= k < N: Multi-turn delta compensation.
            # Replay history[k:N] turns preserving thought steps and signatures, followed by current input.
            payload_input = _format_delta_multi_turn_input(delta_turns, user_content, ordered_steps=versioned_proof)

    elif not is_non_cumulative and (
        effective_mode == "stateful_root_init"
        or (
            effective_mode not in ("disabled", "stateless_full_replay")
            and snapshot.get("continuation_intent_plan", {}).get("continuation_mode") != "off"
            and not fallback
            and binding.get("store_upstream_state") is not False
        )
    ):
        # First turn in a stateful continuation session: genesis anchor
        previous_interaction_id = None
        store = True
        if history:
            payload_input = _format_delta_multi_turn_input(history, user_content, ordered_steps=versioned_proof)
        else:
            payload_input = _format_current_turn_input(user_content)

    else:
        # Stateless or continuation disabled (includes explicit / session_materials_only / fallback)
        previous_interaction_id = None
        store = False
        if not is_non_cumulative and versioned_proof:
            payload_input = _format_delta_multi_turn_input(history, user_content, ordered_steps=True)
        elif history and not is_non_cumulative:
            payload_input = _format_delta_multi_turn_input(history, user_content, ordered_steps=versioned_proof)
        else:
            payload_input = _format_current_turn_input(user_content)

    # 5. Build core payload
    payload: dict[str, Any] = {
        "model": model_name,
        "input": payload_input,
    }

    if previous_interaction_id:
        payload["previous_interaction_id"] = previous_interaction_id

    payload["store"] = store

    head = snapshot.get("normalized_head")
    if head is not None and (store or previous_interaction_id):
        if (snapshot.get("upstream_retention") or {}).get("allow_store") is not True:
            raise ProviderRequestError("CONTINUATION_STORAGE_NOT_AUTHORIZED", "Interaction storage is not authorized")

    # 6. Interaction-scoped system instruction
    instructions = snapshot.get("instructions")
    if not versioned_proof and not instructions:
        instructions = session.get("instructions") or session.get("system_instructions")
    if head is not None:
        if head["instructions"]:
            payload["system_instruction"] = head["instructions"]
    elif instructions:
        if isinstance(instructions, str):
            inst_text = instructions.strip()
        elif isinstance(instructions, dict):
            parts = instructions.get("parts") or []
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
            if texts:
                inst_text = "\n".join(texts).strip()
            elif "text" in instructions:
                inst_text = str(instructions["text"]).strip()
            else:
                inst_text = json.dumps(instructions, ensure_ascii=False).strip()
        else:
            inst_text = str(instructions).strip()

        if inst_text:
            payload["system_instruction"] = inst_text

    # 7. Interaction-scoped tools (normalized symmetrically with parent_head_fingerprint)
    tools = snapshot.get("tools")
    if head is not None:
        if head["tools"]:
            payload["tools"] = head["tools"]
    elif tools and isinstance(tools, list):
        from app.core.context_codec import normalize_native_payload
        unordered = (((snapshot.get("protocol_profile") or {}).get("cache") or {}).get("unordered_tools") is True)
        payload["tools"] = normalize_native_payload(
            {"tools": copy.deepcopy(tools)},
            response_schemas=True,
            unordered_tools=unordered,
        )["tools"]

    # 8. Single-turn generation config
    generation_config: dict[str, Any] = {}
    options = snapshot.get("effective_options") or {}
    if isinstance(options, dict):
        for opt_key, opt_val in options.items():
            if opt_val is not None:
                if opt_key == "temperature":
                    generation_config["temperature"] = float(opt_val)
                elif opt_key == "top_p":
                    generation_config["top_p"] = float(opt_val)
                elif opt_key == "max_output_tokens":
                    generation_config["max_output_tokens"] = int(opt_val)
                elif opt_key == "seed":
                    generation_config["seed"] = int(opt_val)
                else:
                    generation_config[opt_key] = opt_val

    if generation_config:
        payload["generation_config"] = generation_config
    # Use the same frozen-semantics projection as deployment qualification.
    project_thinking(payload, snapshot, "gemini_interactions")

    # 9. Structured output: inject schema constraint at input tail without sending response_format.
    spec = resolve_structured_output(
        snapshot,
        fallback_name="structured_output" if normalization_active() else str(snapshot.get("metadata", {}).get("stage") or "structured_output"),
    )
    if spec is not None:
        if spec.mode == "json_schema" and isinstance(spec.schema, dict):
            payload["input"] = _append_interactions_schema_guidance(payload["input"], spec)
        elif spec.mode != "json_object":
            raise ProviderRequestError("STRUCTURED_OUTPUT_MODE_UNSUPPORTED", "Unsupported Interactions output intent")

    return payload, user_content



def project_gemini_interactions_response_format(spec: StructuredOutputSpec | None) -> dict[str, Any] | None:
    """Interactions never emits response_format so Google implicit prefix cache stays intact."""
    if spec is None:
        return None
    if spec.mode in {"json_object", "json_schema"}:
        return None
    raise ProviderRequestError("STRUCTURED_OUTPUT_MODE_UNSUPPORTED", "Unsupported Interactions output intent")


def _append_interactions_schema_guidance(
    payload_input: list[dict[str, Any]] | str,
    spec: StructuredOutputSpec,
) -> list[dict[str, Any]] | str:
    """Append schema instructions to the current user tail while keeping system_instruction and history untouched."""
    guidance = format_structured_output_prompt_guidance(spec)
    if not guidance:
        return payload_input
    if isinstance(payload_input, str):
        return append_structured_output_prompt(payload_input, guidance)
    if not isinstance(payload_input, list) or not payload_input:
        return [{"type": "text", "text": guidance}]

    updated = copy.deepcopy(payload_input)
    last = updated[-1]
    if isinstance(last, dict) and last.get("type") == "user_input" and isinstance(last.get("content"), list):
        blocks = last["content"]
        if blocks and isinstance(blocks[-1], dict) and blocks[-1].get("type") == "text":
            blocks[-1]["text"] = append_structured_output_prompt(str(blocks[-1].get("text") or ""), guidance)
        else:
            blocks.append({"type": "text", "text": guidance})
    elif isinstance(last, dict) and last.get("type") == "text":
        last["text"] = append_structured_output_prompt(str(last.get("text") or ""), guidance)
    else:
        updated.append({"type": "text", "text": guidance})
    return updated


def _format_current_turn_input(user_content: dict[str, Any]) -> list[dict[str, Any]] | str:
    """Format single current turn user input for the Interactions API."""
    parts = user_content.get("parts") or []
    # If single text part and no materials, can simplify to plain text or single item
    if len(parts) == 1 and "text" in parts[0] and not any(k in parts[0] for k in ("fileData", "inlineData")):
        return str(parts[0]["text"])

    return [project_interactions_content_part(part) for part in parts]


def project_interactions_content_part(part: dict[str, Any]) -> dict[str, Any]:
    """Translate a Gemini native material part into a typed Interactions block.

    Both Files API URIs and inline bytes use the same MIME-derived content
    type. Never send generateContent's generic fileData/inlineData vocabulary.
    """
    if "text" in part:
        return {"type": "text", "text": str(part["text"])}
    if "fileData" in part:
        value, source_key, target_key = part["fileData"], "fileUri", "uri"
        content = value.get(source_key) or value.get("uri")
    elif "inlineData" in part:
        value, target_key = part["inlineData"], "data"
        content = value.get("data")
    else:
        return copy.deepcopy(part)
    mime = str(value.get("mimeType") or "application/octet-stream").split(";", 1)[0].strip().lower()
    media_type = mime.split("/", 1)[0]
    kind = media_type if media_type in {"image", "audio", "video"} else "document"
    return {"type": kind, target_key: content, "mime_type": mime}


def _format_delta_multi_turn_input(
    delta_turns: list[dict[str, Any]],
    current_user_content: dict[str, Any],
    *, ordered_steps: bool = False,
) -> list[dict[str, Any]]:
    """Expand delta turns into an ordered multi-turn input sequence preserving thought signatures."""
    steps: list[dict[str, Any]] = []

    for turn in delta_turns:
        transport = turn.get("transport_history") if isinstance(turn, dict) else {}
        if not isinstance(transport, dict):
            transport = {}

        kind = transport.get("kind")

        # 1. User turn steps
        if kind == "gemini_interactions" and "interaction_user_input" in transport:
            u_input = transport["interaction_user_input"]
            steps.extend(_extract_user_steps(u_input, ordered_steps=ordered_steps))
        elif "user_content" in transport:
            steps.extend(_extract_user_steps(transport["user_content"], ordered_steps=ordered_steps))

        # 2. Model turn steps (thought + signature + model_output)
        if kind == "gemini_interactions" and "interaction_model_steps" in transport:
            for s in transport["interaction_model_steps"]:
                if ordered_steps and (not isinstance(s, dict) or not s.get("type")
                                      or (s["type"] == "thought" and not s.get("signature"))):
                    raise ProviderRequestError("CONTINUATION_NATIVE_STEP_INVALID", "Historical Interactions step is missing its native evidence")
                if not ordered_steps or s["type"] != "user_input":
                    steps.append(copy.deepcopy(s))
        elif "model_content" in transport:
            # Native gemini fallback: extract parts including thought and thoughtSignature
            mc = transport["model_content"]
            mc_parts = mc.get("parts") or []
            for p in mc_parts:
                if p.get("thought") is True:
                    thought_step: dict[str, Any] = {
                        "type": "thought",
                        "text": p.get("text", ""),
                    }
                    if "thoughtSignature" in p:
                        thought_step["signature"] = p["thoughtSignature"]
                    elif "signature" in p:
                        thought_step["signature"] = p["signature"]
                    steps.append(thought_step)
                elif "text" in p:
                    steps.append({"type": "model_output", "text": p["text"]})
                else:
                    steps.append({"type": "step", **p})

    # Finally append current turn user input
    current_steps = _extract_user_steps(current_user_content, ordered_steps=ordered_steps)
    steps.extend(current_steps)
    return steps


def _extract_user_steps(user_content: Any, *, ordered_steps: bool = False) -> list[dict[str, Any]]:
    """Extract standard user input steps from user_content dictionary or string."""
    if isinstance(user_content, dict):
        blocks = [project_interactions_content_part(part) for part in user_content.get("parts") or []]
    else:
        blocks = [{"type": "text", "text": str(user_content)}]
    return [{"type": "user_input", "content": blocks}] if ordered_steps else blocks
