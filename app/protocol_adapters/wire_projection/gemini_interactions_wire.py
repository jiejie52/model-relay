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

from app.canonical_schema import StructuredOutputSpec, resolve_structured_output
from app.core.context_codec import canonical_structure
from app.observability.http_wire_logger import normalization_active
from app.protocol_adapters.base import ProviderRequestError
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
        if bound_delta is not None and isinstance(bound_delta, list):
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
            payload_input = _format_delta_multi_turn_input(delta_turns, user_content)

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
            payload_input = _format_delta_multi_turn_input(history, user_content)
        else:
            payload_input = _format_current_turn_input(user_content)

    else:
        # Stateless or continuation disabled (includes explicit / session_materials_only / fallback)
        previous_interaction_id = None
        store = False
        if history and not is_non_cumulative:
            payload_input = _format_delta_multi_turn_input(history, user_content)
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

    # 6. Interaction-scoped system instruction
    instructions = (
        snapshot.get("instructions")
        or session.get("instructions")
        or session.get("system_instructions")
    )
    if instructions:
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

    # 7. Interaction-scoped tools
    tools = snapshot.get("tools")
    if tools and isinstance(tools, list):
        payload["tools"] = copy.deepcopy(tools)

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

    # Apply thinking semantics
    thinking = snapshot.get("thinking")
    if isinstance(thinking, dict):
        effective = thinking.get("effective") or {}
        wire_action = effective.get("wire_action")
        level = effective.get("level")
        budget = effective.get("budget_tokens")
        if wire_action == "send_level" and level:
            generation_config["thinking_level"] = str(level).upper()
        elif wire_action == "send_budget" and budget is not None:
            generation_config["thinking_budget"] = int(budget)
        elif wire_action == "send_toggle" and level == "disabled":
            generation_config["thinking_budget"] = 0

    if generation_config:
        payload["generation_config"] = generation_config

    # 9. Structured output / response format
    spec = resolve_structured_output(
        snapshot,
        fallback_name="structured_output" if normalization_active() else str(snapshot.get("metadata", {}).get("stage") or "structured_output"),
    )
    response_format = project_gemini_interactions_response_format(spec)
    if response_format is not None:
        payload["response_format"] = response_format

    return payload, user_content



def project_gemini_interactions_response_format(spec: StructuredOutputSpec | None) -> dict[str, Any] | None:
    """Map Relay output intent to Interactions TextResponseFormat.

    Interactions takes JSON Schema under text/schema, not generateContent's
    responseSchema and not an OpenAI json_object/json_schema discriminator.
    The canonical schema is copied intact, including refs and nullable unions;
    Relay still validates the original constraints after receiving the result.
    """
    if spec is None:
        return None
    if spec.mode == "json_object":
        schema = {"type": "object"}
    elif spec.mode == "json_schema" and isinstance(spec.schema, dict):
        schema = copy.deepcopy(spec.schema)
    else:
        raise ProviderRequestError("STRUCTURED_OUTPUT_MODE_UNSUPPORTED", "Unsupported Interactions output intent")
    return {"type": "text", "mime_type": "application/json", "schema": schema}


def _format_current_turn_input(user_content: dict[str, Any]) -> list[dict[str, Any]] | str:
    """Format single current turn user input for the Interactions API."""
    parts = user_content.get("parts") or []
    # If single text part and no materials, can simplify to plain text or single item
    if len(parts) == 1 and "text" in parts[0] and not any(k in parts[0] for k in ("fileData", "inlineData")):
        return str(parts[0]["text"])

    steps: list[dict[str, Any]] = []
    for part in parts:
        if "text" in part:
            steps.append({"type": "text", "text": str(part["text"])})
        elif "fileData" in part:
            fd = part["fileData"]
            steps.append({
                "type": "file",
                "uri": fd.get("fileUri") or fd.get("uri"),
                "mime_type": fd.get("mimeType"),
            })
        elif "inlineData" in part:
            id_data = part["inlineData"]
            steps.append({
                "type": "inline_data",
                "mime_type": id_data.get("mimeType"),
                "data": id_data.get("data"),
            })
        else:
            steps.append(dict(part))
    return steps


def _format_delta_multi_turn_input(
    delta_turns: list[dict[str, Any]],
    current_user_content: dict[str, Any],
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
            steps.extend(_extract_user_steps(u_input))
        elif "user_content" in transport:
            steps.extend(_extract_user_steps(transport["user_content"]))

        # 2. Model turn steps (thought + signature + model_output)
        if kind == "gemini_interactions" and "interaction_model_steps" in transport:
            for s in transport["interaction_model_steps"]:
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
    current_steps = _extract_user_steps(current_user_content)
    steps.extend(current_steps)
    return steps


def _extract_user_steps(user_content: Any) -> list[dict[str, Any]]:
    """Extract standard user input steps from user_content dictionary or string."""
    if isinstance(user_content, str):
        return [{"type": "text", "text": user_content}]
    if not isinstance(user_content, dict):
        return [{"type": "text", "text": str(user_content)}]

    parts = user_content.get("parts") or []
    res: list[dict[str, Any]] = []
    for part in parts:
        if "text" in part:
            res.append({"type": "text", "text": str(part["text"])})
        elif "fileData" in part:
            fd = part["fileData"]
            res.append({
                "type": "file",
                "uri": fd.get("fileUri") or fd.get("uri"),
                "mime_type": fd.get("mimeType"),
            })
        elif "inlineData" in part:
            id_data = part["inlineData"]
            res.append({
                "type": "inline_data",
                "mime_type": id_data.get("mimeType"),
                "data": id_data.get("data"),
            })
        else:
            res.append(dict(part))
    return res
