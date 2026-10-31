from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.cache.economics import (
    estimate_ttl_bounded_future_reuses,
    evaluate_relative_cache_strategy,
)
from app.cache.intent_resolver import CacheIntentResolver
from app.cache.orchestrator import CacheOrchestrator
from app.cache.contracts import ExecutionFence
from app.cache.stateful import StatefulResourceManager
from app.config import Settings
from app.v2_repository import RelayV2Repository


def _relative_config(**overrides):
    value = {
        "cache_read_input_ratio": 0.1,
        "explicit_create_input_ratio": 1.0,
        "safety_margin_ratio": 0.2,
        "expected_future_reuses": 1,
        "certified_implicit_alternative": True,
        "observed_implicit_hit_rate": 0.0,
        "observed_implicit_hit_rate_source": "same_session_provider_usage",
    }
    value.update(overrides)
    return value


def _context_plan():
    return {
        "context_plan_hash": "ctx-hash",
        "stable_prefix_segment_ids": ["stable-instructions"],
        "token_assessment": {"status": "measured", "lower": 4096, "upper": 4096},
    }


def _resolver_inputs(*, reuse_evidence=None, economics=None):
    mechanisms = ["stateful_resource", "implicit_prefix"]
    contract = {
        "verification_status": "verified",
        "supported_mechanisms": mechanisms,
        "mechanism_profiles": {
            "stateful_resource": {
                "threshold_mode": "tokens",
                "minimum_cacheable_tokens": 1024,
                "ttl_seconds": 3600,
                "final_threshold_guard": False,
            },
            "implicit_prefix": {
                "threshold_mode": "tokens",
                "minimum_cacheable_tokens": 1024,
                "final_threshold_guard": False,
            },
        },
    }
    profile = {"cache": {"supported_mechanisms": mechanisms}}
    policy = {
        "scope": "session",
        "mechanism_preference": ["stateful_resource", "implicit_prefix"],
        "auto_prepare_failure": "uncached_same_context",
        "metadata": {"auto_economics": {"safety_margin_ratio": 0.2, **(economics or {})}},
    }
    return {
        "requested_mode": "auto",
        "requested_mechanism": "auto",
        "mechanism_explicit": False,
        "context_plan": _context_plan(),
        "cache_contract": contract,
        "protocol_profile": profile,
        "cache_policy": policy,
        "cache_contract_hash": "contract-hash",
        "profile_hash": "profile-hash",
        "policy_hash": "policy-hash",
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "reuse_evidence": reuse_evidence or {},
    }


def test_relative_economics_uses_input_equivalent_price_model():
    result = evaluate_relative_cache_strategy(
        tokens=100_000,
        ttl_seconds=3600,
        config=_relative_config(expected_future_reuses=1),
        forced=False,
    )
    assert result["pricing_model"] == "input_equivalent/1"
    assert result["cache_read_input_ratio"] == 0.1
    assert result["explicit_create_input_ratio"] == 1.0
    assert result["estimated_creation_cost_input_equivalents"] == 0.1
    assert result["allow"] is True
    invalid_override = evaluate_relative_cache_strategy(
        tokens=100_000,
        ttl_seconds=3600,
        config=_relative_config(cache_read_input_ratio=0.2),
        forced=False,
    )
    assert invalid_override["allow"] is False
    assert invalid_override["reason"] == "relative_price_policy_invalid"


def test_relative_economics_keeps_implicit_when_hit_rate_is_high_or_reuse_is_low():
    high_hit = evaluate_relative_cache_strategy(
        tokens=100_000,
        ttl_seconds=3600,
        config=_relative_config(expected_future_reuses=8, observed_implicit_hit_rate=0.9),
        forced=False,
    )
    low_reuse = evaluate_relative_cache_strategy(
        tokens=100_000,
        ttl_seconds=3600,
        config=_relative_config(expected_future_reuses=0, observed_implicit_hit_rate=0.0),
        forced=False,
    )
    assert high_hit["allow"] is False
    assert low_reuse["allow"] is False
    assert high_hit["reason"] == "explicit_not_economical_vs_implicit"


def test_relative_economics_does_not_guess_when_implicit_hit_rate_is_unknown():
    result = evaluate_relative_cache_strategy(
        tokens=100_000,
        ttl_seconds=3600,
        config=_relative_config(observed_implicit_hit_rate=None, expected_future_reuses=8),
        forced=False,
    )
    assert result["allow"] is False
    assert result["reason"] == "implicit_hit_rate_unverified"


def test_relative_economics_does_not_bypass_hard_budget_for_forced_explicit():
    result = evaluate_relative_cache_strategy(
        tokens=100_000,
        ttl_seconds=3600,
        config=_relative_config(
            max_new_resource_cost=0.001,
            price_schedule={
                "currency": "USD",
                "price_revision": "test/1",
                "input_cost_per_million_tokens": 2.0,
                "storage_cost_per_million_tokens_minute": 0.02,
            },
        ),
        forced=True,
    )
    assert result["allow"] is False
    assert result["reason"] == "new_resource_budget_exceeded"


def test_ttl_forecast_uses_same_session_cadence_and_stops_when_stale():
    forecast = estimate_ttl_bounded_future_reuses(
        policy={"minimum_observations": 3, "max_history_forecast": 8, "history_forecast_confidence": 0.5},
        evidence={
            "observed_compatible_uses": 4,
            "observed_window_seconds": 1800,
            "observed_age_seconds": 30,
        },
        ttl_seconds=3600,
    )
    stale = estimate_ttl_bounded_future_reuses(
        policy={},
        evidence={
            "observed_compatible_uses": 8,
            "observed_window_seconds": 60,
            "observed_age_seconds": 7200,
        },
        ttl_seconds=3600,
    )
    assert forecast["expected_future_reuses"] == 3
    assert forecast["source"] == "same_session_observed_cadence_within_ttl"
    assert stale["expected_future_reuses"] == 0


@pytest.mark.asyncio
async def test_stateful_manager_applies_relative_economics_before_any_create_side_effect():
    events = []

    class FakeAdapter:
        adapter_version = "fake-relative/1"

        def build_spec(self, **kwargs):
            return {
                "cacheable": True,
                "content_fingerprint": "fp-relative",
                "reuse_key": "reuse-relative",
                "prefix_version": 0,
                "compatible_prefix_fingerprints": {"0": "fp-relative"},
                "projection_version": "projector/9",
                "measurement_order": "before_lookup",
                "ttl_seconds": 3600,
            }

        async def measure(self, *, spec):
            events.append("measure")
            return 4096

        async def create(self, *, spec, operation):
            events.append("create")
            raise AssertionError("uneconomical explicit resources must not be created")

    class Registry:
        def maybe_get(self, connection_id):
            return FakeAdapter()

    class Repo:
        settings = Settings(
            _env_file=None,
            relay_api_token="test-token",
            supabase_url="https://example.supabase.co",
            supabase_secret_key="test-secret",
        )

        @staticmethod
        def cache_scope_hash(**kwargs):
            return "scope-relative"

        async def find_compatible_cache_resources(self, **kwargs):
            return []

    result = await StatefulResourceManager(Repo(), Registry()).prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        plan={
            "requested_mode": "auto",
            "requested_mechanism": "auto",
            "allow_uncached_same_context": True,
            "profile_hash": "profile-hash",
            "resolver_version": "relay-cache-resolver/6",
            "mechanism_config": {
                "ttl_seconds": 3600,
                "minimum_cacheable_tokens": 1024,
                "auto_economics": {
                    "new_economics_policy": "relative_cache/2",
                    "cache_read_input_ratio": 0.1,
                    "explicit_create_input_ratio": 1.0,
                    "safety_margin_ratio": 0.2,
                    "expected_future_reuses": 0,
                    "certified_implicit_alternative": True,
                    "observed_implicit_hit_rate": 0.9,
                },
            },
        },
        context_plan={"context_plan_hash": "ctx-hash"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=1),
        snapshot={"connection_id": "conn", "offering_id": "offering", "model": "gemini-3.8-flash"},
        session={"metadata": {"_relay_route": {"account_scope_hash": "account"}}},
        history=[],
    )
    assert events == ["measure"]
    assert result["mechanism"] is None
    assert result["decision_reason"] == "explicit_not_economical_vs_implicit"
    assert result["metadata"]["cache_cost_decision"]["allow"] is False


def test_resolver_freezes_implicit_as_pre_dispatch_fallback_for_explicit_primary():
    result = CacheIntentResolver().resolve(
        **_resolver_inputs(
            reuse_evidence={
                "observed_compatible_uses": 5,
                "observed_implicit_hit_rate": 0.0,
                "observed_implicit_hit_rate_source": "provider_cached_tokens_over_prompt_tokens",
                "observed_implicit_samples": 4,
                "observed_window_seconds": 60,
                "observed_age_seconds": 10,
            }
        )
    )
    assert result.planned_mechanism == "stateful_resource"
    assert result.resolver_version == "relay-cache-resolver/7"
    assert result.reuse_prediction["expected_future_reuses"] == 8
    assert [item["mechanism"] for item in result.fallback_mechanisms] == ["implicit_prefix"]
    assert result.canonical()["fallback_mechanisms"][0]["resolution_status"] == "finalized"


def test_resolver_prefers_implicit_when_observed_implicit_hit_rate_is_high():
    result = CacheIntentResolver().resolve(
        **_resolver_inputs(
            reuse_evidence={
                "observed_compatible_uses": 5,
                "observed_implicit_hit_rate": 0.9,
                "observed_implicit_hit_rate_source": "provider_cached_tokens_over_prompt_tokens",
                "observed_implicit_samples": 4,
                "observed_window_seconds": 1800,
                "observed_age_seconds": 10,
            }
        )
    )
    assert result.planned_mechanism == "implicit_prefix"
    assert result.fallback_mechanisms == ()
    assert result.decision_reason == "selected_implicit_history_default"


def test_resolver_keeps_implicit_when_hit_rate_has_not_been_measured_yet():
    result = CacheIntentResolver().resolve(
        **_resolver_inputs(
            reuse_evidence={
                "observed_compatible_uses": 5,
                "observed_implicit_samples": 0,
                "observed_implicit_hit_rate": None,
                "observed_window_seconds": 60,
                "observed_age_seconds": 10,
            }
        )
    )
    assert result.planned_mechanism == "implicit_prefix"
    assert result.fallback_mechanisms == ()


def test_resolver_never_freezes_an_uncertified_implicit_fallback():
    inputs = _resolver_inputs(
        reuse_evidence={
            "existing_resource_candidate": True,
            "observed_compatible_uses": 4,
            "observed_window_seconds": 600,
            "observed_age_seconds": 10,
            "observed_implicit_hit_rate": 0.0,
        }
    )
    inputs["cache_contract"]["supported_mechanisms"] = ["stateful_resource"]
    inputs["cache_contract"]["mechanism_profiles"].pop("implicit_prefix")
    inputs["protocol_profile"]["cache"]["supported_mechanisms"] = ["stateful_resource"]
    result = CacheIntentResolver().resolve(**inputs)
    assert result.planned_mechanism == "stateful_resource"
    assert result.fallback_mechanisms == ()


def test_legacy_gemini_admission_does_not_infer_implicit_support_from_unverified_contract():
    inputs = _resolver_inputs(
        reuse_evidence={
            "existing_resource_candidate": True,
            "observed_compatible_uses": 4,
            "observed_window_seconds": 600,
            "observed_age_seconds": 10,
            "observed_implicit_hit_rate": 0.0,
        }
    )
    inputs["cache_contract"]["verification_status"] = "unverified"
    result = CacheIntentResolver().resolve(**inputs)
    assert result.planned_mechanism == "stateful_resource"
    assert result.fallback_mechanisms == ()


@pytest.mark.asyncio
async def test_orchestrator_falls_back_to_implicit_before_dispatch_when_explicit_is_uneconomical():
    orchestrator = CacheOrchestrator(repo=object())

    async def fake_stateful_prepare(**kwargs):
        return {
            "mechanism": None,
            "decision_reason": "explicit_not_economical_vs_implicit",
            "metadata": {
                "cache_cost_decision": {
                    "allow": False,
                    "reason": "explicit_not_economical_vs_implicit",
                }
            },
        }

    orchestrator.stateful.prepare = fake_stateful_prepare
    physical = {
        "layout_version": "gemini-physical-cache-layout/9",
        "projector_version": "gemini-physical-projector/9",
        "physical_plan_hash": "physical-plan-hash",
        "implicit_prefix_eligible": True,
        "implicit_prefix_fingerprint": "implicit-prefix-fingerprint",
        "implicit_prefix_payload_hash": "implicit-payload-hash",
        "implicit_prefix_payload": {"contents": [{"role": "user", "parts": [{"text": "full input"}]}]},
        "dependencies": {"safe_history_prefix_version": 0},
    }
    binding = await orchestrator.prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        snapshot={
            "provider": "gemini",
            "connection_id": "conn",
            "cache_plan_hash": "plan-hash",
            "cache_plan": {
                "resolver_version": "relay-cache-resolver/6",
                "requested_mode": "auto",
                "planned_mechanism": "stateful_resource",
                "decision_reason": "selected_high_frequency_reuse",
                "fallback_mechanisms": [{
                    "mechanism": "implicit_prefix",
                    "mechanism_config": {},
                    "resolution_status": "finalized",
                }],
            },
        },
        session={"id": "s"},
        context_plan={"context_plan_hash": "ctx-hash"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=1),
        history=[],
        provider_physical_plan=physical,
    )
    assert binding.mechanism == "implicit_prefix"
    assert binding.decision_reason == "explicit_not_economical_fallback_to_implicit_prefix"
    assert binding.metadata["fallback_from"] == "stateful_resource"
    assert binding.metadata["cache_cost_decision"]["allow"] is False


@pytest.mark.asyncio
async def test_orchestrator_does_not_fallback_after_unknown_explicit_create_side_effect():
    orchestrator = CacheOrchestrator(repo=object())

    async def fake_stateful_prepare(**kwargs):
        return {
            "mechanism": None,
            "decision_reason": "cache_create_unknown",
            "metadata": {"prepare_error_type": "ProviderHTTPError"},
        }

    orchestrator.stateful.prepare = fake_stateful_prepare
    binding = await orchestrator.prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        snapshot={
            "provider": "gemini",
            "connection_id": "conn",
            "cache_plan_hash": "plan-hash",
            "cache_plan": {
                "resolver_version": "relay-cache-resolver/6",
                "requested_mode": "auto",
                "planned_mechanism": "stateful_resource",
                "fallback_mechanisms": [{
                    "mechanism": "implicit_prefix",
                    "mechanism_config": {},
                    "resolution_status": "finalized",
                }],
            },
        },
        session={"id": "s"},
        context_plan={"context_plan_hash": "ctx-hash"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=1),
        history=[],
        provider_physical_plan={"layout_version": "gemini-physical-cache-layout/9"},
    )
    assert binding.mechanism is None
    assert binding.decision_reason == "cache_create_unknown"


@pytest.mark.asyncio
async def test_repository_aggregates_only_matching_session_implicit_usage():
    now = datetime.now(timezone.utc)
    key = "same-context-key"
    rows = [
        {
            "id": "r1",
            "status": "succeeded",
            "created_at": (now - timedelta(minutes=20)).isoformat(),
            "metadata": {"_relay_cache": {"reuse_evidence_key": key, "planned_mechanism": "implicit_prefix"}},
            "cache_usage": {
                "effective_cache_mechanism": "implicit_prefix",
                "cache_read_tokens": 500,
                "prompt_tokens_total": 1000,
                "actual_cache_hit_status": "hit",
            },
        },
        {
            "id": "r2",
            "status": "succeeded",
            "created_at": (now - timedelta(minutes=10)).isoformat(),
            "metadata": {"_relay_cache": {"reuse_evidence_key": key, "planned_mechanism": "implicit_prefix"}},
            "cache_usage": {
                "effective_cache_mechanism": "implicit_prefix",
                "cache_read_tokens": 0,
                "prompt_tokens_total": 1000,
                "actual_cache_hit_status": "miss",
            },
        },
        {
            "id": "r3",
            "status": "succeeded",
            "created_at": now.isoformat(),
            "metadata": {"_relay_cache": {"reuse_evidence_key": key, "planned_mechanism": "stateful_resource"}},
            "cache_usage": {
                "effective_cache_mechanism": "stateful_resource",
                "cache_read_tokens": 1000,
                "prompt_tokens_total": 1000,
                "actual_cache_hit_status": "hit",
            },
        },
        {
            "id": "other-session-context",
            "status": "succeeded",
            "created_at": now.isoformat(),
            "metadata": {"_relay_cache": {"reuse_evidence_key": "different-key", "planned_mechanism": "implicit_prefix"}},
            "cache_usage": {
                "effective_cache_mechanism": "implicit_prefix",
                "cache_read_tokens": 1000,
                "prompt_tokens_total": 1000,
                "actual_cache_hit_status": "hit",
            },
        },
    ]

    class Backend:
        async def select(self, *args, **kwargs):
            assert kwargs["select"] == "id,metadata,cache_usage,created_at,status"
            return rows

    repo = RelayV2Repository(
        Backend(),
        Settings(
            _env_file=None,
            relay_api_token="test-token",
            supabase_url="https://example.supabase.co",
            supabase_secret_key="test-secret",
        ),
    )
    evidence = await repo.recent_session_cache_reuse_evidence(
        session_id="s", tenant_id="t", conversation_hash="c", reuse_evidence_key=key,
    )
    assert evidence["observed_compatible_uses"] == 3
    assert evidence["observed_stateful_compatible_uses"] == 1
    assert evidence["observed_implicit_uses"] == 2
    assert evidence["observed_implicit_samples"] == 2
    assert evidence["observed_implicit_hit_rate"] == 0.25
    assert evidence["observed_implicit_request_hit_rate"] == 0.5
    assert evidence["observed_implicit_hit_rate_source"] == "provider_cached_tokens_over_prompt_tokens"
    assert evidence["observed_window_seconds"] == pytest.approx(1200, abs=1)
