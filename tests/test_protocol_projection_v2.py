from __future__ import annotations

from app.providers.chat_completions import ChatCompletionsV2Adapter
from app.providers.claude_messages import ClaudeMessagesV2Adapter
from app.providers.openai_compatible import OpenAICompatibleResponsesProvider
from app.providers.http_wire import observed_response_facts


def test_responses_reasoning_projection_uses_contract_strategy():
    payload = {}
    snapshot = {
        "capability_contract": {
            "thinking": {
                "wire_strategy": "responses_reasoning_effort",
                "include_encrypted_reasoning": False,
            }
        }
    }
    OpenAICompatibleResponsesProvider._apply_reasoning(
        payload,
        snapshot,
        provider="openai",
        model="gpt-6-sol",
        think_level="high",
        session=None,
    )
    assert payload["reasoning"] == {"effort": "high"}


def test_chat_thinking_projection_for_glm_and_configured_toggle_contract():
    glm = {}
    ChatCompletionsV2Adapter._apply_thinking(
        glm,
        {
            "think_level": "max",
            "capability_contract": {
                "thinking": {"wire_strategy": "chat_reasoning_effort_with_thinking_on"}
            },
        },
    )
    assert glm["thinking"] == {"type": "enabled"}
    assert glm["reasoning_effort"] == "max"

    mimo = {}
    ChatCompletionsV2Adapter._apply_thinking(
        mimo,
        {
            "think_level": "off",
            "capability_contract": {"thinking": {"wire_strategy": "chat_thinking_toggle"}},
        },
    )
    assert mimo["thinking"] == {"type": "disabled"}


def test_claude_output_config_combines_effort_and_schema():
    payload = {}
    snapshot = {
        "think_level": "high",
        "capability_contract": {"thinking": {"wire_strategy": "claude_adaptive_effort"}},
        "structured_output_guarantee": "native_json_schema",
        "structured_output": {
            "mode": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
        "metadata": {},
    }
    ClaudeMessagesV2Adapter._apply_thinking_and_output(payload, snapshot)
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"]["effort"] == "high"
    assert payload["output_config"]["format"]["type"] == "json_schema"


def test_aihubmix_observed_headers_override_body_model_and_preserve_gateway_facts():
    observed = observed_response_facts(
        {
            "X-Aihubmix-Fallback": "true",
            "X-Aihubmix-Model": "gpt-6-luna",
            "X-Aihubmix-Router-Resolved-Model": "gpt-6-sol",
            "X-JSON-Repaired": "true",
        },
        body_model="gpt-6-sol",
        channel_id="aihubmix",
        protocol="responses",
    )
    assert observed["actual_model"] == "gpt-6-luna"
    assert observed["response_model"] == "gpt-6-sol"
    assert observed["aihubmix_fallback"] is True
    assert observed["aihubmix_router_resolved_model"] == "gpt-6-sol"
    assert observed["json_repaired"] is True
