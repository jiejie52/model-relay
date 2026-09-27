from __future__ import annotations

from types import SimpleNamespace
import json

import pytest

from app.config import Settings
from app.control_plane import ModelControlPlane
from app.model_options import ModelOptionError, resolve_model_options
from app.core.execution_runtime import SharedExecutionRuntime
from app.providers.base import ProviderRequestError
from app.providers.factory import register_protocol_adapters
from app.providers.registry import ProviderRegistry
from app.routing.catalog import RouteCatalog, RouteEntry
from app.routing.route_resolver import RouteResolver


def settings(**overrides):
    values = {
        "relay_api_token": "test-token",
        "supabase_url": "https://example.supabase.co",
        "supabase_secret_key": "test-secret",
        "aihubmix_api_key": "aihub-key",
        "aihubmix_gemini_base_url": "https://aihubmix.com/gemini",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_builtin_control_plane_contains_target_models_and_protocols():
    cp = ModelControlPlane.from_settings(settings())
    expected = {
        "claude-opus-5-5": "claude_messages",
        "claude-sonnet-5": "claude_messages",
        "grok-4.7": "responses",
        "gpt-6-luna": "responses",
        "gpt-6-sol": "responses",
        "gpt-6-astra": "responses",
        "coding-glm-5.3-free": "chat_completions",
        "xiaomi-mimo-v2.6-pro-free": "chat_completions",
        "gemini-3.8-flash": "gemini_native",
    }
    actual = {}
    for offering in cp.offerings.values():
        if offering.model_pattern in expected:
            actual[offering.model_pattern] = cp.connection(offering.connection_id).protocol
    assert actual == expected
    assert len(cp.control_plane_hash) == 64


def test_gpt6_auto_is_frozen_and_sampling_requires_no_reasoning():
    cp = ModelControlPlane.from_settings(settings())
    contract = cp.contract("gpt-6-reasoning").canonical()
    resolved = resolve_model_options(
        provider="openai",
        model="gpt-6-sol",
        options={"max_output_tokens": 4096},
        think_level="auto",
        capability_contract=contract,
    )
    assert resolved.requested_think_level == "auto"
    assert resolved.effective_think_level == "medium"

    with pytest.raises(ModelOptionError) as exc:
        resolve_model_options(
            provider="openai",
            model="gpt-6-sol",
            options={"temperature": 0.2},
            think_level="high",
            capability_contract=contract,
        )
    assert exc.value.code == "OPTION_CONFLICT"

    no_reasoning = resolve_model_options(
        provider="openai",
        model="gpt-6-sol",
        options={"temperature": 0.2},
        think_level="none",
        capability_contract=contract,
    )
    assert no_reasoning.effective_options["temperature"] == 0.2
    assert no_reasoning.effective_think_level == "none"


def test_free_model_thinking_contracts_are_not_faked_as_one_enum():
    cp = ModelControlPlane.from_settings(settings())
    glm = resolve_model_options(
        provider="glm",
        model="coding-glm-5.3-free",
        options={},
        think_level="auto",
        capability_contract=cp.contract("glm-5-3-free-chat").canonical(),
    )
    assert glm.effective_think_level == "max"

    mimo = resolve_model_options(
        provider="xiaomi",
        model="xiaomi-mimo-v2.6-pro-free",
        options={},
        think_level="auto",
        capability_contract=cp.contract("mimo-v2-6-chat").canonical(),
    )
    assert mimo.effective_think_level == "auto"

    with pytest.raises(ModelOptionError) as exc:
        resolve_model_options(
            provider="xiaomi",
            model="xiaomi-mimo-v2.6-pro-free",
            options={},
            think_level="high",
            capability_contract=cp.contract("mimo-v2-6-chat").canonical(),
        )
    assert exc.value.code == "THINK_LEVEL_UNSUPPORTED"


def test_claude_contract_applies_model_specific_required_default():
    cp = ModelControlPlane.from_settings(settings())
    resolved = resolve_model_options(
        provider="anthropic",
        model="claude-sonnet-5",
        options={},
        think_level="high",
        capability_contract=cp.contract("claude-sonnet-5-adaptive").canonical(),
    )
    assert resolved.requested_options == {}
    assert resolved.effective_options["max_output_tokens"] == 8192
    assert resolved.effective_think_level == "high"

    sonnet_auto = resolve_model_options(
        provider="anthropic",
        model="claude-sonnet-5",
        options={},
        think_level="auto",
        capability_contract=cp.contract("claude-sonnet-5-adaptive").canonical(),
    )
    assert sonnet_auto.effective_think_level == "high"

    opus_auto = resolve_model_options(
        provider="anthropic",
        model="claude-opus-5-5",
        options={},
        think_level="auto",
        capability_contract=cp.contract("claude-opus-5-5-adaptive").canonical(),
    )
    assert opus_auto.effective_think_level == "medium"

    sonnet_off = resolve_model_options(
        provider="anthropic",
        model="claude-sonnet-5",
        options={},
        think_level="off",
        capability_contract=cp.contract("claude-sonnet-5-adaptive").canonical(),
    )
    assert sonnet_off.effective_think_level == "off"

    with pytest.raises(ModelOptionError):
        resolve_model_options(
            provider="anthropic",
            model="claude-opus-5-5",
            options={},
            think_level="off",
            capability_contract=cp.contract("claude-opus-5-5-adaptive").canonical(),
        )


class DummySettings:
    deployment_id = "test"
    execution_pool = "test-pool"
    route_legacy_hint_mode = "warn"
    connection_availability_mode = "all"

    def connection_is_enabled(self, connection_id):
        return True

    def connection_configuration(self, connection_id):
        if connection_id == "high":
            return False, "high unavailable"
        return True, None

    def connection_account_scope_hash(self, connection_id):
        return f"scope-{connection_id}"


class DummyProviders:
    def describe(self, connection_id):
        if connection_id == "low":
            return {
                "provider": "",
                "protocol": "responses",
                "adapter_version": "test/1",
            }
        return None

    def registered_connections(self):
        return ["low"]


class DummyFiles:
    def describe(self, connection_id):
        return None

    def registered_connections(self):
        return []


def test_route_priority_is_selection_before_session_freeze_not_runtime_failover():
    contract = {
        "contract_id": "c1",
        "revision": "c/1",
        "supported_options": {},
        "thinking": {"accepted_levels": ["auto"]},
        "structured_output": {"mode": "none"},
        "input_modalities": ["text"],
    }
    catalog = RouteCatalog(
        revision="r1",
        entries=[
            RouteEntry(
                provider="openai",
                model_pattern="gpt-x",
                connection_id="high",
                priority=200,
                offering_id="offer-high",
                channel_id="channel-high",
                protocol="responses",
                capability_contract_id="c1",
                capability_contract=contract,
                source="control_plane",
            ),
            RouteEntry(
                provider="openai",
                model_pattern="gpt-x",
                connection_id="low",
                priority=100,
                offering_id="offer-low",
                channel_id="channel-low",
                protocol="responses",
                capability_contract_id="c1",
                capability_contract=contract,
                source="control_plane",
            ),
        ],
    )
    resolver = RouteResolver(
        settings=DummySettings(),
        catalog=catalog,
        providers=DummyProviders(),
        provider_files=DummyFiles(),
    )
    binding = resolver.resolve(provider="openai", model="gpt-x", purpose="session")
    assert binding.connection_id == "low"
    assert binding.offering_id == "offer-low"
    assert binding.channel_id == "channel-low"


def test_session_capability_requirements_filter_candidates():
    cp = ModelControlPlane.from_settings(settings())
    chat = cp.contract("glm-5-3-free-chat")
    ok, reason = chat.supports_requirements(
        {"input_modalities": ["text"], "structured_output": "post_validate"}
    )
    assert ok and reason is None
    ok, reason = chat.supports_requirements(
        {"input_modalities": ["image"], "structured_output": "post_validate"}
    )
    assert not ok and "unsupported_input_modalities" in reason


def test_custom_control_plane_requires_published_status_and_sorts_offerings():
    draft = {
        "status": "review",
        "revision": "cp/review-1",
        "offerings": [],
    }
    with pytest.raises(ValueError, match="status=published"):
        ModelControlPlane.from_settings(settings(model_control_plane_json=json.dumps(draft)))

    published = {
        "status": "published",
        "revision": "cp/prod-1",
        "release_metadata": {"release_id": "release-17", "approved_by": "test"},
        "connections": [
            {
                "connection_id": "direct-responses",
                "channel_id": "direct",
                "protocol": "responses",
                "base_url": "https://example.invalid/v1",
                "credential_env": "DIRECT_TEST_KEY"
            }
        ],
        "capability_contracts": [
            {
                "contract_id": "test-responses",
                "revision": "contract/1",
                "supported_options": {},
                "thinking": {"mode": "none", "accepted_levels": ["auto"]},
                "structured_output": {"mode": "none"},
                "input_modalities": ["text"]
            }
        ],
        "offerings": [
            {
                "offering_id": "test-low",
                "provider": "openai",
                "model_pattern": "gpt-test",
                "connection_id": "direct-responses",
                "capability_contract_id": "test-responses",
                "priority": 50
            },
            {
                "offering_id": "test-high",
                "provider": "openai",
                "model_pattern": "gpt-test",
                "connection_id": "aihubmix_default",
                "capability_contract_id": "test-responses",
                "priority": 250
            }
        ]
    }
    cp = ModelControlPlane.from_settings(settings(model_control_plane_json=json.dumps(published)))
    assert cp.status == "published"
    assert cp.release_metadata["release_id"] == "release-17"
    matches = cp.candidate_offerings(provider="openai", model="gpt-test", deployment_id="railway")
    assert [x.offering_id for x in matches] == ["test-high", "test-low"]


def test_strict_observed_model_contract_fails_after_preserving_success_refs():
    with pytest.raises(ProviderRequestError) as exc_info:
        SharedExecutionRuntime._check_observed_contract(
            {
                "provider": "openai",
                "model": "gpt-6-sol",
                "offering_id": "aihubmix-gpt-6-sol",
                "channel_id": "aihubmix",
                "protocol": "responses",
                "observed_model_policy": "strict",
            },
            {
                "actual_model": "gpt-6-luna",
                "aihubmix_fallback": True,
                "protocol": "responses",
                "channel_id": "aihubmix",
            },
            request_id="req-test",
            session_id="ses-test",
            raw_object_id="obj-raw",
            output_object_id="obj-output",
        )
    exc = exc_info.value
    assert exc.code == "UPSTREAM_MODEL_CONTRACT_MISMATCH"
    assert getattr(exc, "provider_success_object_id") == "obj-raw"
    assert getattr(exc, "provider_output_object_id") == "obj-output"


def test_audit_observed_model_contract_does_not_change_execution_result():
    SharedExecutionRuntime._check_observed_contract(
        {"model": "gpt-a", "observed_model_policy": "audit"},
        {"actual_model": "gpt-b"},
        request_id="req-test",
        session_id="ses-test",
        raw_object_id="obj-raw",
        output_object_id="obj-output",
    )


def test_zero_code_custom_responses_connection_registers_from_published_config(monkeypatch):
    monkeypatch.setenv("DIRECT_TEST_KEY", "direct-secret")
    published = {
        "status": "published",
        "revision": "cp/zero-code",
        "connections": [
            {
                "connection_id": "direct-responses",
                "channel_id": "direct",
                "protocol": "responses",
                "base_url": "https://example.invalid/v1",
                "credential_env": "DIRECT_TEST_KEY"
            }
        ],
        "capability_contracts": [
            {
                "contract_id": "zero-code-contract",
                "revision": "contract/zero-code",
                "supported_options": {"max_output_tokens": {"type": "integer", "min": 1}},
                "thinking": {"mode": "none", "accepted_levels": ["auto"]},
                "structured_output": {"mode": "native_json_schema"},
                "input_modalities": ["text"]
            }
        ],
        "offerings": [
            {
                "offering_id": "zero-code-model",
                "provider": "openai",
                "model_pattern": "gpt-zero-code",
                "connection_id": "direct-responses",
                "capability_contract_id": "zero-code-contract",
                "priority": 300
            }
        ]
    }
    cfg = settings(model_control_plane_json=json.dumps(published))
    cp = ModelControlPlane.from_settings(cfg)
    providers = ProviderRegistry(cfg)
    register_protocol_adapters(
        settings=cfg,
        control_plane=cp,
        providers=providers,
        materials=object(),
        repo=object(),
    )
    meta = providers.describe("direct-responses")
    assert meta is not None
    assert meta["protocol"] == "responses"
    assert meta["channel_id"] == "direct"
