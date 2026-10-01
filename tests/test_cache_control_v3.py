from __future__ import annotations

from copy import deepcopy

import pytest

from app.cache.context_plan import build_context_plan
from app.cache.contracts import CacheDecisionError
from app.cache.intent_resolver import CacheIntentResolver
from app.cache.prefix import prepare_implicit_prefix_binding
from app.cache.usage import normalize_cache_usage
from app.config import Settings
from app.control_plane import ModelControlPlane
from app.core.idempotency import caller_intent_hash, request_identity
from app.providers.openai_compatible import OpenAICompatibleResponsesProvider
from app.v2_models import ExecutionSpec, SessionRequestCreate


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


def _context_plan(*, with_stable_prefix: bool = True, measured: tuple[int | None, int | None] | None = None):
    session = {
        "id": "s1",
        "context_policy": "conversation",
        "material_manifest": ["m1"] if with_stable_prefix else [],
        "history_version": 0,
        "history_object_id": None,
    }
    snapshot = {"instructions": "stable" if with_stable_prefix else None, "input": "dynamic"}
    materials = (
        [{"id": "m1", "filename": "a.txt", "content_type": "text/plain", "sha256": "abc"}]
        if with_stable_prefix
        else []
    )
    plan = build_context_plan(snapshot=snapshot, session=session, material_rows=materials)
    if measured is not None:
        lower, upper = measured
        plan["token_assessment"] = {
            "status": "measured",
            "lower": lower,
            "upper": upper,
            "source": "test",
            "estimator_version": "test/1",
        }
    return plan


def test_new_request_contract_normalizes_omitted_cache_mode_to_auto():
    body = SessionRequestCreate(input="x", execution=ExecutionSpec(mode="sync"))
    assert body.requested_cache_mode == "auto"
    assert "requested_cache_mode" not in body.model_fields_set

    explicit = SessionRequestCreate(
        input="x", execution=ExecutionSpec(mode="sync"), requested_cache_mode="auto"
    )
    assert explicit.requested_cache_mode == "auto"
    assert "requested_cache_mode" in explicit.model_fields_set


def test_builtin_cache_certification_does_not_infer_capability_from_protocol_name():
    cp = ModelControlPlane.from_settings(settings())
    grok = next(x for x in cp.offerings.values() if x.model_pattern == "grok-4.7")
    gemini = next(x for x in cp.offerings.values() if x.model_pattern == "gemini-3.8-flash")
    gpt = next(x for x in cp.offerings.values() if x.model_pattern == "gpt-6-sol")

    grok_cache = cp.contract(grok.capability_contract_id).cache
    gemini_cache = cp.contract(gemini.capability_contract_id).cache
    gpt_cache = cp.contract(gpt.capability_contract_id).cache
    assert grok_cache["verification_status"] == "legacy_verified"
    assert grok_cache["supported_mechanisms"] == ["implicit_prefix"]
    assert gemini_cache["verification_status"] == "candidate"
    assert gpt_cache["verification_status"] == "candidate"


def test_cache_resolver_off_never_requires_provider_capability():
    plan = CacheIntentResolver().resolve(
        requested_mode="off",
        context_plan=_context_plan(),
        cache_contract=None,
        protocol_profile=None,
        cache_policy=None,
        cache_contract_hash=None,
        profile_hash=None,
        policy_hash=None,
    )
    assert plan.planned_mechanism is None
    assert plan.decision_reason == "requested_off"


def test_cache_resolver_on_unsupported_is_fail_closed_but_auto_is_none():
    resolver = CacheIntentResolver()
    with pytest.raises(CacheDecisionError) as exc:
        resolver.resolve(
            requested_mode="on",
            context_plan=_context_plan(),
            cache_contract={"supported_mechanisms": []},
            protocol_profile={},
            cache_policy={},
            cache_contract_hash="c",
            profile_hash="p",
            policy_hash="q",
        )
    assert exc.value.code == "CACHE_UNSUPPORTED"

    auto = resolver.resolve(
        requested_mode="auto",
        context_plan=_context_plan(),
        cache_contract={"supported_mechanisms": []},
        protocol_profile={},
        cache_policy={},
        cache_contract_hash="c",
        profile_hash="p",
        policy_hash="q",
    )
    assert auto.planned_mechanism is None
    assert auto.decision_reason == "mechanism_unavailable"


def test_cache_resolver_on_below_threshold_is_none_not_error():
    resolver = CacheIntentResolver()
    result = resolver.resolve(
        requested_mode="on",
        context_plan=_context_plan(measured=(100, 120)),
        cache_contract={
            "supported_mechanisms": ["breakpoint"],
            "verification_status": "verified",
            "mechanism_profiles": {
                "breakpoint": {"threshold_mode": "tokens", "minimum_cacheable_tokens": 1024}
            },
        },
        protocol_profile={"cache": {"supported_mechanisms": ["breakpoint"]}},
        cache_policy={"scope": "session", "mechanism_preference": ["breakpoint"]},
        cache_contract_hash="c",
        profile_hash="p",
        policy_hash="q",
    )
    assert result.planned_mechanism is None
    assert result.decision_reason == "below_minimum"


def test_cache_resolver_unknown_threshold_is_conservative_none():
    result = CacheIntentResolver().resolve(
        requested_mode="on",
        context_plan=_context_plan(),
        cache_contract={
            "supported_mechanisms": ["breakpoint"],
            "verification_status": "verified",
            "mechanism_profiles": {"breakpoint": {"threshold_mode": "unknown"}},
        },
        protocol_profile={"cache": {"supported_mechanisms": ["breakpoint"]}},
        cache_policy={"scope": "session", "mechanism_preference": ["breakpoint"]},
        cache_contract_hash="c",
        profile_hash="p",
        policy_hash="q",
    )
    assert result.planned_mechanism is None
    assert result.decision_reason == "context_assessment_uncertain"


def test_context_plan_keeps_dynamic_input_out_of_stable_prefix():
    plan = _context_plan()
    by_id = {x["segment_id"]: x for x in plan["segments"]}
    stable = [by_id[x] for x in plan["stable_prefix_segment_ids"]]
    assert stable
    assert all(x["domain"] != "input" for x in stable)
    assert plan["segments"][-1]["domain"] == "input"
    assert plan["segments"][-1]["stability"] == "dynamic_suffix"


def test_v23_identity_changes_with_requested_cache_intent_but_not_runtime_handle():
    base = {
        "schema_version": "relay-request/2.3",
        "session_id": "s",
        "tenant_id": "t",
        "conversation_hash": "c",
        "input": "x",
        "instructions": None,
        "material_ids": [],
        "material_hashes": {},
        "provider": "grok",
        "connection_id": "conn",
        "model": "grok-4.7",
        "think_level": "auto",
        "structured_output": {},
        "execution": {"mode": "sync"},
        "options": {},
        "effective_options": {},
        "capability_revision": "r",
        "route_binding_hash": "route",
        "capability_contract_hash": "contract",
        "protocol_profile_hash": "profile",
        "cache_policy_hash": "policy",
        "cache_contract_hash": "cache-contract",
        "context_policy": "conversation",
        "history_version": 0,
        "effective_material_ids": [],
        "cache_plan_hash": "plan",
        "context_plan_hash": "context",
    }
    on = {**base, "requested_cache_mode": "on"}
    off = {**base, "requested_cache_mode": "off"}
    assert request_identity(on) != request_identity(off)

    physical = deepcopy(on)
    physical["_relay_cache_execution"] = {
        "provider_handle": "provider/one",
        "resource_generation": 17,
        "actual_cache_hit_status": "hit",
    }
    assert request_identity(physical) == request_identity(on)


def test_v22_identity_algorithm_does_not_start_hashing_new_cache_fields():
    old = {
        "schema_version": "relay-request/2.2",
        "session_id": "s",
        "tenant_id": "t",
        "conversation_hash": "c",
        "input": "x",
        "instructions": None,
        "material_ids": [],
        "material_hashes": {},
        "provider": "openai",
        "connection_id": "conn",
        "model": "gpt-x",
        "think_level": "auto",
        "structured_output": {},
        "execution": {"mode": "sync"},
        "options": {},
        "effective_options": {},
        "capability_revision": "r",
    }
    augmented = {**old, "requested_cache_mode": "off", "cache_plan_hash": "new"}
    assert request_identity(old) == request_identity(augmented)


def test_unknown_request_identity_version_fails_closed():
    with pytest.raises(ValueError, match="Unsupported Relay Request identity version"):
        request_identity({"schema_version": "relay-request/2.99"})


def test_grok_legacy_key_is_used_as_implicit_prefix_binding_and_off_gate_removes_only_key():
    session = {"id": "s", "prompt_cache_key": "dify-relay-existing-key"}
    prefix = prepare_implicit_prefix_binding(
        plan={"mechanism_config": {"key_source": "legacy_session_prompt_cache_key"}},
        context_plan={"stable_prefix_fingerprint": "abc"},
        session=session,
    )
    assert prefix["prompt_cache_key"] == "dify-relay-existing-key"

    payload = {
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "high"},
        "prompt_cache_key": "dify-relay-existing-key",
    }
    snapshot = {
        "schema_version": "relay-request/2.3",
        "_relay_cache_execution": {"mechanism": None},
    }
    OpenAICompatibleResponsesProvider._apply_cache_projection(
        payload, snapshot, provider="grok", model="grok-4.7"
    )
    assert "prompt_cache_key" not in payload
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["reasoning"] == {"effort": "high"}


def test_implicit_prefix_projection_is_independent_of_reasoning():
    payload = {"store": False}
    snapshot = {
        "schema_version": "relay-request/2.3",
        "_relay_cache_execution": {
            "mechanism": "implicit_prefix",
            "prefix": {"prompt_cache_key": "relay-cache-frozen"},
        },
    }
    OpenAICompatibleResponsesProvider._apply_cache_projection(
        payload, snapshot, provider="openai", model="gpt-x"
    )
    assert payload["prompt_cache_key"] == "relay-cache-frozen"


def test_cache_usage_normalization_preserves_null_vs_zero_and_transparent_hit():
    unknown = normalize_cache_usage(
        protocol="responses", usage={}, effective_mechanism="implicit_prefix", requested_mode="auto"
    )
    assert unknown.cache_read_tokens is None
    assert unknown.actual_cache_hit_status == "unknown"

    miss = normalize_cache_usage(
        protocol="responses",
        usage={"input_tokens_details": {"cached_tokens": 0}},
        effective_mechanism="implicit_prefix",
        requested_mode="auto",
    )
    assert miss.cache_read_tokens == 0
    assert miss.actual_cache_hit_status == "miss"

    transparent = normalize_cache_usage(
        protocol="responses",
        usage={"input_tokens_details": {"cached_tokens": 33}},
        effective_mechanism=None,
        requested_mode="off",
    )
    assert transparent.actual_cache_hit_status == "hit"
    assert transparent.transparent_observation is True


def test_caller_intent_hash_treats_auto_consistently_and_on_off_differ():
    base = {
        "input": "x",
        "instructions": None,
        "material_ids": [],
        "provider": "grok",
        "model": "grok-4.7",
        "think_level": "auto",
        "execution": {"mode": "sync"},
        "structured_output": {},
        "options": {},
        "metadata": {},
    }
    assert caller_intent_hash({**base, "requested_cache_mode": "auto"}) == caller_intent_hash(
        {**base, "requested_cache_mode": "auto"}
    )
    assert caller_intent_hash({**base, "requested_cache_mode": "on"}) != caller_intent_hash(
        {**base, "requested_cache_mode": "off"}
    )


def test_caller_intent_hash_detects_provider_payload_migration_input_changes():
    base = {
        "input": "x",
        "instructions": None,
        "material_ids": [],
        "provider": "grok",
        "model": "grok-4.7",
        "think_level": "auto",
        "execution": {"mode": "sync"},
        "structured_output": {},
        "options": {},
        "requested_cache_mode": "auto",
        "metadata": {},
    }
    assert caller_intent_hash({**base, "provider_payload": {"temperature": 0.2}}) != caller_intent_hash(
        {**base, "provider_payload": {"temperature": 0.3}}
    )


def test_cache_operation_singleflight_key_is_request_independent():
    import asyncio
    from types import SimpleNamespace
    from app.v2_repository import RelayV2Repository

    class Backend:
        def __init__(self):
            self.calls = []

        async def rpc(self, name, args):
            self.calls.append((name, args))
            return [{
                "op_id": "00000000-0000-0000-0000-000000000001",
                "lease_owner": args["p_lease_owner"],
                "lease_epoch": 1,
            }]

    backend = Backend()
    repo = RelayV2Repository(backend, SimpleNamespace(cache_prepare_timeout_seconds=60))

    async def run():
        await repo.create_cache_operation_intent(
            request_id="00000000-0000-0000-0000-000000000011",
            scope_hash="s" * 64,
            content_fingerprint="f" * 64,
            operation_type="create",
            lease_owner="worker-a",
            lease_epoch=1,
        )
        await repo.create_cache_operation_intent(
            request_id="00000000-0000-0000-0000-000000000022",
            scope_hash="s" * 64,
            content_fingerprint="f" * 64,
            operation_type="create",
            lease_owner="worker-b",
            lease_epoch=2,
        )

    asyncio.run(run())
    first = backend.calls[0][1]["p_idempotency_key"]
    second = backend.calls[1][1]["p_idempotency_key"]
    assert first == second
    assert "00000000-0000-0000-0000-000000000011" not in first


def test_ready_cache_resource_skips_expired_generation():
    import asyncio
    from types import SimpleNamespace
    from app.v2_repository import RelayV2Repository

    class Backend:
        async def select(self, table, **kwargs):
            assert table == "relay_cache_resources"
            return [
                {
                    "id": "expired",
                    "generation": 3,
                    "provider_handle_ref": "cachedContents/expired",
                    "expire_time": "2000-01-01T00:00:00+00:00",
                },
                {
                    "id": "ready",
                    "generation": 2,
                    "provider_handle_ref": "cachedContents/ready",
                    "expire_time": "2999-01-01T00:00:00+00:00",
                },
            ]

    repo = RelayV2Repository(Backend(), SimpleNamespace())
    row = asyncio.run(
        repo.find_ready_cache_resource(scope_hash="s", content_fingerprint="f")
    )
    assert row["id"] == "ready"


def test_cache_sql_contains_recovery_and_dispatch_fencing_contracts():
    from pathlib import Path

    sql = Path("sql/005_relay_cache_control.sql").read_text()
    for token in (
        "relay_cache_resources",
        "relay_cache_operations",
        "relay_request_cache_bindings",
        "relay_cache_pins",
        "relay_cache_tasks",
        "accept_relay_request_v3",
        "install_request_material_binding_v3",
        "install_cache_binding_v3",
        "seal_cache_and_dispatch_v3",
        "store_relay_result_v3",
        "complete_relay_request_v3",
        "fail_relay_request_v3",
    ):
        assert token in sql
    assert "provider_dispatch_state='dispatch_started'" in sql
