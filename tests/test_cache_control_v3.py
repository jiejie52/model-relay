from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import json
import logging

import httpx
import pytest

from app.cache.context_plan import build_context_plan
from app.cache.contracts import CacheDecisionError
from app.cache.intent_resolver import CacheIntentResolver
from app.cache.providers import GeminiAIHubMixCacheResourceAdapter
from app.cache.stateful import StatefulResourceManager
from app.cache.contracts import ExecutionFence
from app.cache.prefix import prepare_implicit_prefix_binding
from app.cache.usage import normalize_cache_usage
from app.config import Settings
from app.control_plane import ModelControlPlane
from app.core.idempotency import caller_intent_hash, request_identity
from app.core.provider_error_observation import provider_http_error_observation
from app.providers.base import ProviderHTTPError
from app.providers.openai_compatible import OpenAICompatibleResponsesProvider
from app.providers.gemini_native import GeminiNativeAdapter
from app.providers.gemini_physical import (
    GEMINI_PHYSICAL_LAYOUT_VERSION,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V4,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V3,
    GEMINI_SESSION_PROJECTION_METADATA_KEY,
    build_gemini_physical_cache_plan,
    frozen_session_projection_metadata,
)
from app.providers.v2_base import V2ExecutionContext
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
    gemini_lite = next(x for x in cp.offerings.values() if x.model_pattern == "gemini-3.1-flash-lite")
    generic_gemini = cp.offerings["legacy-gemini-aihubmix-native"]
    gpt = next(x for x in cp.offerings.values() if x.model_pattern == "gpt-6-sol")

    grok_cache = cp.contract(grok.capability_contract_id).cache
    gemini_cache = cp.contract(gemini.capability_contract_id).cache
    gemini_lite_cache = cp.contract(gemini_lite.capability_contract_id).cache
    generic_gemini_cache = cp.contract(generic_gemini.capability_contract_id).cache
    gpt_cache = cp.contract(gpt.capability_contract_id).cache
    assert grok_cache["verification_status"] == "legacy_verified"
    assert grok_cache["supported_mechanisms"] == ["implicit_prefix"]
    assert gemini_cache["verification_status"] == "verified"
    assert gemini_lite_cache["verification_status"] == "verified"
    # Verification is model/Supply specific. The gemini-* catch-all remains
    # closed so image or future models are never inferred cache-capable merely
    # because they use the Gemini native protocol.
    assert generic_gemini_cache["verification_status"] == "candidate"
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



def test_provider_count_final_guard_allows_planning_before_measurement():
    result = CacheIntentResolver().resolve(
        requested_mode="on",
        context_plan=_context_plan(),
        cache_contract={
            "supported_mechanisms": ["stateful_resource"],
            "verification_status": "verified",
            "mechanism_profiles": {
                "stateful_resource": {
                    "threshold_mode": "provider_count",
                    "minimum_cacheable_tokens": 1024,
                    "final_threshold_guard": True,
                }
            },
        },
        protocol_profile={"cache": {"supported_mechanisms": ["stateful_resource"]}},
        cache_policy={"scope": "session", "mechanism_preference": ["stateful_resource"]},
        cache_contract_hash="c",
        profile_hash="p",
        policy_hash="q",
    )
    assert result.planned_mechanism == "stateful_resource"
    assert result.final_threshold_guard is True
    assert result.resolution_status == "pending"
    assert result.decision_reason == "final_threshold_pending"


def _gemini_history():
    return [
        {
            "transport_history": {
                "kind": "gemini_native",
                "user_content": {"role": "user", "parts": [{"text": "u1"}]},
                "model_content": {"role": "model", "parts": [{"text": "a1"}]},
            }
        },
        {
            "transport_history": {
                "kind": "gemini_native",
                "user_content": {"role": "user", "parts": [{"text": "u2"}]},
                "model_content": {"role": "model", "parts": [{"text": "a2"}]},
            }
        },
    ]


def test_gemini_cache_spec_is_committed_prefix_only_and_has_compatible_boundaries():
    adapter = GeminiAIHubMixCacheResourceAdapter(settings())
    snapshot = {
        "model": "gemini-3.1-flash-lite",
        "instructions": "system",
        "input": "CURRENT-INPUT-MUST-NOT-BE-CACHED",
    }
    history = _gemini_history()
    spec = adapter.build_spec(
        snapshot=snapshot,
        history=history,
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[],
        session={"context_policy": "conversation"},
        ttl_seconds=3600,
    )
    assert spec["prefix_version"] == 2
    assert spec["provider_payload"]["systemInstruction"]["parts"][0]["text"] == "system"
    wire_text = json.dumps(spec["provider_payload"])
    assert "u1" in wire_text and "a2" in wire_text
    assert "CURRENT-INPUT-MUST-NOT-BE-CACHED" not in wire_text
    assert set(spec["compatible_prefix_fingerprints"]) == {"0", "1", "2"}
    assert spec["compatible_prefix_fingerprints"]["1"] != spec["compatible_prefix_fingerprints"]["2"]


def test_gemini_generate_payload_uses_cached_prefix_and_sends_only_history_suffix():
    context = V2ExecutionContext(
        snapshot={
            "schema_version": "relay-request/2.3",
            "model": "gemini-3.1-flash-lite",
            "instructions": "system",
            "input": "current",
        },
        session={"context_policy": "conversation"},
        history=_gemini_history(),
        material_ids=[],
        material_bindings=[],
        tenant_id="t",
        conversation_hash="c",
        cache_execution_binding={
            "mechanism": "stateful_resource",
            "provider_handle": "cachedContents/cache-123",
            "metadata": {"cached_history_version": 1},
        },
    )
    payload, user_content = GeminiNativeAdapter._build_context_payload(context)
    assert payload["cachedContent"] == "cachedContents/cache-123"
    assert "systemInstruction" not in payload
    # Cached prefix owns turn 1. Only turn 2 + current user are transmitted.
    assert payload["contents"] == [
        {"role": "user", "parts": [{"text": "u2"}]},
        {"role": "model", "parts": [{"text": "a2"}]},
        {"role": "user", "parts": [{"text": "current"}]},
    ]
    assert user_content == {"role": "user", "parts": [{"text": "current"}]}


def test_gemini_uncached_payload_still_rebuilds_full_logical_context():
    context = V2ExecutionContext(
        snapshot={
            "schema_version": "relay-request/2.3",
            "model": "gemini-3.1-flash-lite",
            "instructions": "system",
            "input": "current",
        },
        session={"context_policy": "conversation"},
        history=_gemini_history(),
        material_ids=[],
        material_bindings=[],
        tenant_id="t",
        conversation_hash="c",
        cache_execution_binding={"mechanism": None},
    )
    payload, _ = GeminiNativeAdapter._build_context_payload(context)
    assert payload["systemInstruction"]["parts"][0]["text"] == "system"
    assert len(payload["contents"]) == 5
    assert payload["contents"][-1]["parts"][0]["text"] == "current"


@pytest.mark.asyncio
async def test_gemini_cachedcontent_crud_and_counttokens_wire_contract():
    calls: list[tuple[str, str, dict | None, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        calls.append((request.method, request.url.path, body, str(request.url.query)))
        if request.url.path.endswith(":countTokens"):
            assert body["generateContentRequest"]["model"] == "models/gemini-3.1-flash-lite"
            return httpx.Response(200, json={"totalTokens": 2048})
        if request.method == "POST" and request.url.path.endswith("/v1beta/cachedContents"):
            assert body["model"] == "models/gemini-3.1-flash-lite"
            return httpx.Response(
                200,
                json={
                    "name": "cachedContents/cache-123",
                    "expireTime": "2099-01-01T00:00:00Z",
                    "usageMetadata": {"totalTokenCount": 2048},
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json={"name": "cachedContents/cache-123", "expireTime": "2099-01-01T00:00:00Z"})
        if request.method == "PATCH":
            assert "updateMask=expire_time" in str(request.url)
            return httpx.Response(200, json={"name": "cachedContents/cache-123", "expireTime": body["expireTime"]})
        if request.method == "DELETE":
            return httpx.Response(200, content=b"")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    adapter = GeminiAIHubMixCacheResourceAdapter(
        settings(), transport=httpx.MockTransport(handler)
    )
    spec = adapter.build_spec(
        snapshot={"model": "gemini-3.1-flash-lite", "instructions": "system", "input": "current"},
        history=_gemini_history()[:1],
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[],
        session={"context_policy": "conversation"},
        ttl_seconds=600,
    )
    assert await adapter.measure(spec=spec) == 2048
    created = await adapter.create(spec=spec, operation={"lease_epoch": 7})
    assert created["handle"] == "cachedContents/cache-123"
    assert created["operation_epoch"] == 7
    got = await adapter.get(handle=created["handle"])
    assert got["handle"] == created["handle"]
    renewed = await adapter.renew(handle=created["handle"], expire_at="2099-02-01T00:00:00Z")
    assert renewed["expire_time"] == "2099-02-01T00:00:00Z"
    deleted = await adapter.delete(handle=created["handle"])
    assert deleted["deleted"] is True
    assert [x[0] for x in calls] == ["POST", "POST", "GET", "PATCH", "DELETE"]


@pytest.mark.asyncio
async def test_gemini_generatecontent_surfaces_cached_token_evidence():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1beta/models/gemini-3.1-flash-lite:generateContent")
        body = json.loads(request.content.decode())
        assert body["cachedContent"] == "cachedContents/cache-123"
        assert "systemInstruction" not in body
        raw = json.dumps({
            "responseId": "r1",
            "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 2200,
                "cachedContentTokenCount": 1800,
                "candidatesTokenCount": 10,
                "totalTokenCount": 2210,
            },
        }).encode()

        class _Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield raw

        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Stream())

    adapter = GeminiNativeAdapter(settings(), transport=httpx.MockTransport(handler))
    context = V2ExecutionContext(
        snapshot={
            "schema_version": "relay-request/2.3",
            "model": "gemini-3.1-flash-lite",
            "connection_id": "aihubmix_gemini_native",
            "channel_id": "aihubmix",
            "instructions": "system",
            "input": "current",
            "effective_options": {},
            "think_level": "auto",
            "metadata": {},
        },
        session={"context_policy": "conversation"},
        history=_gemini_history()[:1],
        material_ids=[],
        material_bindings=[],
        tenant_id="t",
        conversation_hash="c",
        cache_execution_binding={
            "mechanism": "stateful_resource",
            "provider_handle": "cachedContents/cache-123",
            "metadata": {"cached_history_version": 1},
        },
    )
    result = await adapter.execute(context)
    assert result.text == "ok"
    assert result.cached_tokens == 1800
    assert result.usage["raw"]["cachedContentTokenCount"] == 1800



@pytest.mark.asyncio
async def test_stateful_manager_reuses_older_compatible_history_prefix_without_recreate():
    class FakeAdapter:
        adapter_version = "fake/1"
        measured = False
        created = False

        def build_spec(self, **kwargs):
            return {
                "cacheable": True,
                "content_fingerprint": "fp-current",
                "reuse_key": "reuse-family",
                "prefix_version": 2,
                "compatible_prefix_fingerprints": {"1": "fp-old", "2": "fp-current"},
                "projection_version": "gemini-cachedcontent-prefix/1",
            }

        async def get(self, *, handle, operation=None):
            assert handle == "cachedContents/old"
            return {"handle": handle, "expire_time": "2099-01-01T00:00:00Z"}

        async def measure(self, *, spec):
            self.measured = True
            raise AssertionError("compatible resource should be reused before countTokens")

        async def create(self, *, spec, operation):
            self.created = True
            raise AssertionError("compatible resource should not be recreated")

    adapter = FakeAdapter()

    class Registry:
        def maybe_get(self, connection_id):
            return adapter if connection_id == "aihubmix_gemini_native" else None

    class Repo:
        settings = settings()

        @staticmethod
        def cache_scope_hash(**kwargs):
            return "scope"

        async def find_compatible_cache_resources(self, **kwargs):
            assert kwargs["reuse_key"] == "reuse-family"
            assert kwargs["max_prefix_version"] == 2
            return [{
                "id": "resource-1",
                "generation": 1,
                "prefix_version": 1,
                "content_fingerprint": "fp-old",
                "provider_handle_ref": "cachedContents/old",
                "expire_time": "2099-01-01T00:00:00Z",
                "token_count": 4096,
            }]

        async def invalidate_cache_resource(self, **kwargs):
            raise AssertionError("healthy resource must not be invalidated")

    manager = StatefulResourceManager(Repo(), Registry())
    result = await manager.prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        plan={
            "requested_mode": "on",
            "profile_hash": "profile",
            "mechanism_config": {"minimum_cacheable_tokens": 1024, "ttl_seconds": 3600},
        },
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=3),
        snapshot={"connection_id": "aihubmix_gemini_native", "offering_id": "o", "model": "gemini-3.1-flash-lite"},
        session={"metadata": {"_relay_route": {"account_scope_hash": "a"}}},
        history=_gemini_history(),
    )
    assert result["decision_reason"] == "resource_reused"
    assert result["provider_handle"] == "cachedContents/old"
    assert result["metadata"]["cached_history_version"] == 1
    assert adapter.measured is False
    assert adapter.created is False


def test_sql_006_declares_v31_fencing_and_compatible_prefix_fields():
    sql = (Path(__file__).parents[1] / "sql" / "006_gemini_stateful_cache.sql").read_text()
    for required in (
        "reuse_key text",
        "prefix_version bigint",
        "publish_cache_resource_v31",
        "start_cache_operation_v31",
        "record_cache_operation_observation_v31",
        "finish_cache_operation_v31",
        "invalidate_cache_resource_v31",
        "cache_resolution_status='finalized'",
        "state='failed'",
    ):
        assert required in sql

@pytest.mark.asyncio
async def test_stateful_manager_create_path_seals_cache_operation_and_persists_handle_first():
    events = []

    class FakeAdapter:
        adapter_version = "fake/1"

        def build_spec(self, **kwargs):
            return {
                "cacheable": True,
                "content_fingerprint": "fp-current",
                "reuse_key": "reuse-family",
                "prefix_version": 1,
                "compatible_prefix_fingerprints": {"1": "fp-current"},
                "projection_version": "gemini-cachedcontent-prefix/1",
            }

        async def measure(self, *, spec):
            events.append("measure")
            return 2048

        async def create(self, *, spec, operation):
            events.append("create")
            return {
                "handle": "cachedContents/new",
                "expire_time": "2099-01-01T00:00:00Z",
                "provider_request_id": "provider-create-1",
                "operation_epoch": operation["lease_epoch"],
            }

        async def get(self, *, handle, operation=None):
            events.append("get")
            return {"handle": handle, "expire_time": "2099-01-01T00:00:00Z"}

    adapter = FakeAdapter()

    class Registry:
        def maybe_get(self, connection_id):
            return adapter

    class Repo:
        settings = settings()

        @staticmethod
        def cache_scope_hash(**kwargs):
            return "scope"

        async def find_compatible_cache_resources(self, **kwargs):
            return []

        async def create_cache_operation_intent(self, **kwargs):
            events.append("claim")
            return {"op_id": "op-1", "lease_owner": "worker", "lease_epoch": 7}

        async def start_cache_operation(self, **kwargs):
            events.append("start")
            assert kwargs["lease_epoch"] == 7
            return True

        async def record_cache_operation_observation(self, **kwargs):
            events.append("record")
            assert kwargs["raw_result"]["handle"] == "cachedContents/new"
            return True

        async def publish_cache_resource(self, **kwargs):
            events.append("publish")
            assert kwargs["provider_handle_ref"] == "cachedContents/new"
            return {
                "id": "resource-1",
                "generation": 1,
                "provider_handle_ref": "cachedContents/new",
                "expire_time": "2099-01-01T00:00:00Z",
            }

        async def finish_cache_operation(self, **kwargs):
            raise AssertionError("successful create path must publish, not finish as failure")

    manager = StatefulResourceManager(Repo(), Registry())
    result = await manager.prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        plan={
            "requested_mode": "on",
            "profile_hash": "profile",
            "mechanism_config": {"minimum_cacheable_tokens": 1024, "ttl_seconds": 3600},
        },
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=3),
        snapshot={"connection_id": "aihubmix_gemini_native", "offering_id": "o", "model": "gemini-3.1-flash-lite"},
        session={"metadata": {"_relay_route": {"account_scope_hash": "a"}}},
        history=_gemini_history()[:1],
    )
    assert result["decision_reason"] == "resource_created"
    assert result["provider_handle"] == "cachedContents/new"
    assert events == ["measure", "claim", "start", "create", "record", "get", "publish"]


@pytest.mark.asyncio
async def test_stateful_manager_post_create_verify_failure_is_unknown_not_retryable_failed():
    finished = []

    class FakeAdapter:
        adapter_version = "fake/1"

        def build_spec(self, **kwargs):
            return {
                "cacheable": True,
                "content_fingerprint": "fp-current",
                "reuse_key": "reuse-family",
                "prefix_version": 1,
                "compatible_prefix_fingerprints": {"1": "fp-current"},
                "projection_version": "gemini-cachedcontent-prefix/1",
            }

        async def measure(self, *, spec):
            return 2048

        async def create(self, *, spec, operation):
            return {
                "handle": "cachedContents/maybe-created",
                "provider_request_id": "provider-create-2",
                "operation_epoch": operation["lease_epoch"],
            }

        async def get(self, *, handle, operation=None):
            raise ProviderHTTPError(404, b"missing", phase="gemini_cache_get")

    class Registry:
        def maybe_get(self, connection_id):
            return FakeAdapter()

    class Repo:
        settings = settings()

        @staticmethod
        def cache_scope_hash(**kwargs):
            return "scope"

        async def find_compatible_cache_resources(self, **kwargs):
            return []

        async def create_cache_operation_intent(self, **kwargs):
            return {"op_id": "op-2", "lease_owner": "worker", "lease_epoch": 9}

        async def start_cache_operation(self, **kwargs):
            return True

        async def record_cache_operation_observation(self, **kwargs):
            return True

        async def finish_cache_operation(self, **kwargs):
            finished.append(kwargs)
            return True

        async def publish_cache_resource(self, **kwargs):
            raise AssertionError("unverified handle must not be published")

    manager = StatefulResourceManager(Repo(), Registry())
    result = await manager.prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        plan={
            "requested_mode": "auto",
            "allow_uncached_same_context": True,
            "profile_hash": "profile",
            "mechanism_config": {"minimum_cacheable_tokens": 1024, "ttl_seconds": 3600},
        },
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=3),
        snapshot={"connection_id": "aihubmix_gemini_native", "offering_id": "o", "model": "gemini-3.1-flash-lite"},
        session={"metadata": {"_relay_route": {"account_scope_hash": "a"}}},
        history=_gemini_history()[:1],
    )
    assert result["mechanism"] is None
    assert result["decision_reason"] == "cache_create_unknown"
    assert finished and finished[0]["state"] == "unknown"
    assert finished[0]["raw_result"]["handle"] == "cachedContents/maybe-created"

def test_gemini_native_cache_reference_is_reserved_from_legacy_provider_payload():
    payload = {"contents": [{"role": "user", "parts": [{"text": "x"}]}]}
    GeminiNativeAdapter._apply_model_options(
        payload,
        {
            "schema_version": "relay-request/2.1",
            "provider_payload": {
                "cachedContent": "cachedContents/caller-injected",
                "temperature": 0.2,
            },
        },
    )
    assert "cachedContent" not in payload
    assert payload["generationConfig"]["temperature"] == 0.2



def _gemini_v2_session(*, material_manifest=None, layout_version=None):
    projection = frozen_session_projection_metadata()
    if layout_version is not None:
        projection = dict(projection)
        projection["layout_version"] = layout_version
        if layout_version == GEMINI_PHYSICAL_LAYOUT_VERSION_V4:
            projection["projector_version"] = "gemini-physical-projector/4"
    return {
        "id": "s-v2",
        "context_policy": "conversation",
        "material_manifest": list(material_manifest or []),
        "metadata": {
            GEMINI_SESSION_PROJECTION_METADATA_KEY: projection,
            "_relay_route": {"account_scope_hash": "acct"},
        },
    }


def _gemini_binding(material_id: str, uri: str, *, sha: str) -> dict:
    return {
        "material_id": material_id,
        "binding_generation": 1,
        "binding_kind": "gemini_file_uri",
        "representation": "gemini_file_uri",
        "connection_id": "aihubmix_gemini_native",
        "account_scope_hash": "acct",
        "provider": "gemini",
        "external_uri": uri,
        "file_uri": uri,
        "external_file_id": f"files/{material_id}",
        "provider_file_id": f"files/{material_id}",
        "content_sha256": sha,
        "content_type": "application/pdf",
        "filename": f"{material_id}.pdf",
    }


def test_gemini_physical_plan_moves_session_material_into_cached_prefix_and_strips_history_occurrence():
    session = _gemini_v2_session(material_manifest=["m-session"])
    snapshot = {
        "schema_version": "relay-request/2.3",
        "model": "gemini-3.1-flash-lite",
        "instructions": "request-stage-instruction",
        "input": "current question",
        "protocol_profile_hash": "profile",
        "capability_contract_hash": "cap",
        "cache_contract_hash": "cache",
    }
    bindings = [
        _gemini_binding("m-session", "https://files.example/session", sha="sha-session"),
        _gemini_binding("m-request", "https://files.example/request", sha="sha-request"),
    ]
    history = [
        {
            "transport_history": {
                "kind": "gemini_native",
                "user_content": {
                    "role": "user",
                    "parts": [
                        {"fileData": {"mimeType": "application/pdf", "fileUri": "https://files.example/session"}},
                        {"text": "old question"},
                    ],
                },
                "model_content": {"role": "model", "parts": [{"text": "old answer"}]},
                "material_occurrences": [
                    {
                        "material_id": "m-session",
                        "part_index": 0,
                        "placement": "cached_prefix",
                        "representation_digest": "digest-session",
                    }
                ],
            }
        }
    ]
    plan = build_gemini_physical_cache_plan(
        snapshot=snapshot,
        session=session,
        history=history,
        material_ids=["m-session", "m-request"],
        material_bindings=bindings,
        context_plan={"context_plan_hash": "ctx"},
    )

    assert plan["layout_version"] == GEMINI_PHYSICAL_LAYOUT_VERSION
    assert plan["cacheable"] is True
    cached_wire = json.dumps(plan["cached_prefix"], ensure_ascii=False)
    assert "https://files.example/session" in cached_wire
    assert "https://files.example/request" not in cached_wire
    assert "request-stage-instruction" not in cached_wire

    suffix_wire = json.dumps(plan["uncached_suffix"], ensure_ascii=False)
    assert "request-stage-instruction" in suffix_wire
    assert "old question" in suffix_wire and "old answer" in suffix_wire
    assert "https://files.example/session" not in suffix_wire
    assert "https://files.example/request" in suffix_wire
    assert "current question" in suffix_wire

    full_wire = json.dumps(plan["full_uncached_payload"], ensure_ascii=False)
    assert "https://files.example/session" in full_wire
    assert "https://files.example/request" in full_wire
    assert "old question" in full_wire and "current question" in full_wire
    assert any(
        x.get("source") == "history"
        and x.get("material_id") == "m-session"
        and x.get("placement") == "cached_prefix"
        for x in plan["occurrence_mapping"]
    )


def test_gemini_v2_cache_spec_consumes_exact_physical_cached_prefix():
    session = _gemini_v2_session(material_manifest=["m-session"])
    snapshot = {
        "schema_version": "relay-request/2.3",
        "model": "gemini-3.1-flash-lite",
        "instructions": "dynamic-stage",
        "input": "question",
    }
    bindings = [_gemini_binding("m-session", "https://files.example/session", sha="sha-session")]
    plan = build_gemini_physical_cache_plan(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-session"],
        material_bindings=bindings,
        context_plan={"context_plan_hash": "ctx"},
    )
    adapter = GeminiAIHubMixCacheResourceAdapter(settings())
    spec = adapter.build_spec(
        snapshot=snapshot,
        history=[],
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=bindings,
        session=session,
        ttl_seconds=600,
        provider_physical_plan=plan,
    )
    assert spec["provider_payload"] == plan["cached_prefix"]
    assert spec["content_fingerprint"] == plan["content_fingerprint"]
    assert spec["reuse_key"] == plan["reuse_key"]
    assert spec["physical_plan_hash"] == plan["physical_plan_hash"]
    assert spec["measurement_order"] == "provider_create"
    assert "dynamic-stage" not in json.dumps(spec["provider_payload"])



def test_gemini_layout_v3_keeps_counttokens_preflight_for_frozen_sessions():
    session = _gemini_v2_session(material_manifest=["m-session"])
    session["metadata"][GEMINI_SESSION_PROJECTION_METADATA_KEY] = {
        "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION_V3,
        "projector_version": "gemini-physical-projector/3",
    }
    snapshot = {
        "schema_version": "relay-request/2.3",
        "model": "gemini-3.1-flash-lite",
        "instructions": "dynamic-stage",
        "input": "question",
    }
    bindings = [_gemini_binding("m-session", "https://files.example/session", sha="sha-session")]
    plan = build_gemini_physical_cache_plan(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-session"],
        material_bindings=bindings,
        context_plan={"context_plan_hash": "ctx"},
    )
    assert plan["layout_version"] == GEMINI_PHYSICAL_LAYOUT_VERSION_V3
    assert plan["measurement_order"] == "before_lookup"
    assert plan["projector_version"] == "gemini-physical-projector/3"


def test_provider_create_threshold_is_finalized_without_counttokens_guard():
    resolver = CacheIntentResolver()
    result = resolver.resolve(
        requested_mode="auto",
        context_plan=_context_plan(with_stable_prefix=True),
        cache_contract={
            "supported_mechanisms": ["stateful_resource"],
            "verification_status": "verified",
            "mechanism_profiles": {
                "stateful_resource": {
                    "threshold_mode": "provider_create",
                    "final_threshold_guard": False,
                    "precreate_measurement": "none",
                    "create_api": "caches.create",
                    "ttl_seconds": 3600,
                }
            },
        },
        protocol_profile={"cache": {"supported_mechanisms": ["stateful_resource"]}},
        cache_policy={
            "scope": "session",
            "mechanism_preference": ["stateful_resource"],
            "auto_prepare_failure": "uncached_same_context",
        },
        cache_contract_hash="cache",
        profile_hash="profile",
        policy_hash="policy",
    )
    assert result.planned_mechanism == "stateful_resource"
    assert result.resolution_status == "finalized"
    assert result.decision_reason == "selected"
    assert result.final_threshold_guard is False
    assert result.mechanism_config["precreate_measurement"] == "none"


@pytest.mark.asyncio
async def test_stateful_manager_provider_create_skips_counttokens_and_uses_create_usage():
    events = []

    class FakeAdapter:
        adapter_version = "fake/4"

        def build_spec(self, **kwargs):
            return {
                "cacheable": True,
                "content_fingerprint": "fp-direct-create",
                "reuse_key": "reuse-direct-create",
                "prefix_version": 0,
                "compatible_prefix_fingerprints": {"0": "fp-direct-create"},
                "projection_version": "gemini-physical-projector/4",
                "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION,
                "measurement_order": "provider_create",
            }

        async def measure(self, *, spec):
            events.append("measure")
            raise AssertionError("layout/4 must not call countTokens")

        async def create(self, *, spec, operation):
            events.append("create")
            return {
                "handle": "cachedContents/direct-create",
                "expire_time": "2099-01-01T00:00:00Z",
                "usage_metadata": {"totalTokenCount": 4096},
                "operation_epoch": operation["lease_epoch"],
            }

        async def get(self, *, handle, operation=None):
            events.append("get")
            return {"handle": handle, "expire_time": "2099-01-01T00:00:00Z"}

    adapter = FakeAdapter()

    class Registry:
        def maybe_get(self, connection_id):
            return adapter

    class Repo:
        settings = settings()

        @staticmethod
        def cache_scope_hash(**kwargs):
            return "scope"

        async def find_compatible_cache_resources(self, **kwargs):
            events.append("find")
            return []

        async def create_cache_operation_intent(self, **kwargs):
            events.append("claim")
            return {"op_id": "op-direct", "lease_owner": "worker", "lease_epoch": 11}

        async def start_cache_operation(self, **kwargs):
            events.append("start")
            return True

        async def record_cache_operation_observation(self, **kwargs):
            events.append("record")
            return True

        async def publish_cache_resource(self, **kwargs):
            events.append("publish")
            assert kwargs["token_count"] == 4096
            return {
                "id": "resource-direct",
                "generation": 1,
                "provider_handle_ref": "cachedContents/direct-create",
                "expire_time": "2099-01-01T00:00:00Z",
                "token_count": 4096,
                "content_fingerprint": "fp-direct-create",
            }

        async def finish_cache_operation(self, **kwargs):
            raise AssertionError("successful direct create must publish")

    result = await StatefulResourceManager(Repo(), Registry()).prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        plan={
            "requested_mode": "on",
            "profile_hash": "profile",
            "mechanism_config": {
                "threshold_mode": "provider_create",
                "precreate_measurement": "none",
                "ttl_seconds": 3600,
            },
        },
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=11),
        snapshot={"connection_id": "conn", "offering_id": "o", "model": "gemini-3.1-flash-lite"},
        session={"metadata": {"_relay_route": {"account_scope_hash": "a"}}},
        history=[],
    )
    assert result["mechanism"] == "stateful_resource"
    assert result["provider_handle"] == "cachedContents/direct-create"
    assert result["metadata"]["prefix_token_count"] == 4096
    assert result["metadata"]["provider_measurement"] == "cachedContents.create.usageMetadata"
    assert events == ["find", "claim", "start", "create", "record", "get", "publish"]


@pytest.mark.asyncio
async def test_layout4_native_wire_is_cachedcontent_create_then_generate_without_counttokens():
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        calls.append((request.method, request.url.path, body))
        assert not request.url.path.endswith(":countTokens")
        if request.method == "POST" and request.url.path.endswith("/v1beta/cachedContents"):
            wire = json.dumps(body, ensure_ascii=False)
            assert "https://files.example/session" in wire
            assert "current question" not in wire
            return httpx.Response(
                200,
                headers={"x-goog-request-id": "cache-create-41"},
                json={
                    "name": "cachedContents/cache-41",
                    "expireTime": "2099-01-01T00:00:00Z",
                    "usageMetadata": {"totalTokenCount": 4096},
                },
            )
        if request.method == "GET" and request.url.path.endswith("/v1beta/cachedContents/cache-41"):
            return httpx.Response(
                200,
                json={
                    "name": "cachedContents/cache-41",
                    "expireTime": "2099-01-01T00:00:00Z",
                },
            )
        if request.method == "POST" and request.url.path.endswith(
            "/v1beta/models/gemini-3.1-flash-lite:generateContent"
        ):
            wire = json.dumps(body, ensure_ascii=False)
            assert body["cachedContent"] == "cachedContents/cache-41"
            assert "current question" in wire
            assert "https://files.example/session" not in wire
            raw = json.dumps({
                "responseId": "response-41",
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "ok"}]}}
                ],
                "usageMetadata": {
                    "promptTokenCount": 4200,
                    "cachedContentTokenCount": 4096,
                    "candidatesTokenCount": 10,
                    "totalTokenCount": 4210,
                },
            }).encode()

            class _Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield raw

            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_Stream(),
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    cache_adapter = GeminiAIHubMixCacheResourceAdapter(settings(), transport=transport)

    class Registry:
        def maybe_get(self, connection_id):
            assert connection_id == "aihubmix_gemini_native"
            return cache_adapter

    class Repo:
        settings = settings()

        @staticmethod
        def cache_scope_hash(**kwargs):
            return "scope-layout4-wire"

        async def find_compatible_cache_resources(self, **kwargs):
            return []

        async def create_cache_operation_intent(self, **kwargs):
            return {"op_id": "op-layout4-wire", "lease_owner": "worker", "lease_epoch": 13}

        async def start_cache_operation(self, **kwargs):
            return True

        async def record_cache_operation_observation(self, **kwargs):
            return True

        async def publish_cache_resource(self, **kwargs):
            return {
                "id": "resource-layout4-wire",
                "generation": 1,
                "provider_handle_ref": kwargs["provider_handle_ref"],
                "expire_time": kwargs["expire_time"],
                "token_count": kwargs["token_count"],
                "content_fingerprint": kwargs["content_fingerprint"],
                "prefix_version": kwargs["prefix_version"],
            }

        async def finish_cache_operation(self, **kwargs):
            raise AssertionError("successful create should not finish as a failed/unknown operation")

    session = _gemini_v2_session(
        material_manifest=["m-session"],
        layout_version=GEMINI_PHYSICAL_LAYOUT_VERSION_V4,
    )
    snapshot = {
        "schema_version": "relay-request/2.3",
        "model": "gemini-3.1-flash-lite",
        "connection_id": "aihubmix_gemini_native",
        "channel_id": "aihubmix",
        "offering_id": "gemini-3-1-flash-lite-aihubmix",
        "instructions": "dynamic stage instruction",
        "input": "current question",
        "effective_options": {},
        "think_level": "auto",
        "metadata": {},
        "protocol_profile_hash": "profile",
        "capability_contract_hash": "cap",
        "cache_contract_hash": "cache",
    }
    bindings = [_gemini_binding("m-session", "https://files.example/session", sha="sha-session")]
    physical = build_gemini_physical_cache_plan(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-session"],
        material_bindings=bindings,
        context_plan={"context_plan_hash": "ctx"},
    )
    binding = await StatefulResourceManager(Repo(), Registry()).prepare(
        request_row={
            "id": "request-layout4-wire",
            "tenant_id": "tenant",
            "conversation_hash": "conversation",
            "session_id": session["id"],
        },
        plan={
            "requested_mode": "on",
            "profile_hash": "profile",
            "mechanism_config": {
                "threshold_mode": "provider_create",
                "precreate_measurement": "none",
                "ttl_seconds": 600,
            },
        },
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=bindings,
        fence=ExecutionFence(owner="worker", epoch=13),
        snapshot=snapshot,
        session=session,
        history=[],
        provider_physical_plan=physical,
    )
    assert binding["provider_handle"] == "cachedContents/cache-41"

    inference = GeminiNativeAdapter(settings(), transport=transport)
    result = await inference.execute(
        V2ExecutionContext(
            snapshot=snapshot,
            session=session,
            history=[],
            material_ids=["m-session"],
            material_bindings=bindings,
            tenant_id="tenant",
            conversation_hash="conversation",
            cache_execution_binding=binding,
            provider_physical_plan=physical,
        )
    )
    assert result.text == "ok"
    assert result.cached_tokens == 4096
    assert [path for _, path, _ in calls] == [
        "/gemini/v1beta/cachedContents",
        "/gemini/v1beta/cachedContents/cache-41",
        "/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent",
    ]


def test_gemini_v2_generate_payload_uses_exact_uncached_suffix_with_cache_handle():
    session = _gemini_v2_session(material_manifest=["m-session"])
    snapshot = {
        "schema_version": "relay-request/2.3",
        "model": "gemini-3.1-flash-lite",
        "instructions": "stage instruction",
        "input": "current",
    }
    bindings = [_gemini_binding("m-session", "https://files.example/session", sha="sha-session")]
    plan = build_gemini_physical_cache_plan(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-session"],
        material_bindings=bindings,
        context_plan={"context_plan_hash": "ctx"},
    )
    context = V2ExecutionContext(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-session"],
        material_bindings=bindings,
        tenant_id="t",
        conversation_hash="c",
        cache_execution_binding={
            "mechanism": "stateful_resource",
            "provider_handle": "cachedContents/cache-v2",
            "metadata": {
                "physical_plan_hash": plan["physical_plan_hash"],
                "uncached_suffix_wire_hash": plan["uncached_suffix_wire_hash"],
            },
        },
        provider_physical_plan=plan,
    )
    payload, history_user = GeminiNativeAdapter._build_context_payload(context)
    expected = deepcopy(plan["uncached_suffix"])
    expected["cachedContent"] = "cachedContents/cache-v2"
    assert payload == expected
    assert "https://files.example/session" not in json.dumps(payload)
    assert history_user == plan["history_entry_user_content"]
    assert "https://files.example/session" in json.dumps(history_user)


def test_gemini_v2_none_resolution_sends_complete_uncached_context():
    session = _gemini_v2_session(material_manifest=["m-session"])
    snapshot = {
        "schema_version": "relay-request/2.3",
        "model": "gemini-3.1-flash-lite",
        "instructions": "stage instruction",
        "input": "current",
    }
    bindings = [_gemini_binding("m-session", "https://files.example/session", sha="sha-session")]
    plan = build_gemini_physical_cache_plan(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-session"],
        material_bindings=bindings,
        context_plan={"context_plan_hash": "ctx"},
    )
    context = V2ExecutionContext(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-session"],
        material_bindings=bindings,
        tenant_id="t",
        conversation_hash="c",
        cache_execution_binding={"mechanism": None},
        provider_physical_plan=plan,
    )
    payload, _ = GeminiNativeAdapter._build_context_payload(context)
    assert payload == plan["full_uncached_payload"]
    assert "cachedContent" not in payload
    assert "https://files.example/session" in json.dumps(payload)


def test_provider_http_error_observation_redacts_body_and_pseudonymizes_request_id():
    exc = ProviderHTTPError(
        503,
        b'{"error":"Bearer topsecret","api_key":"AIza123456789012345678901234567890","nested":{"password":"p@ss"}}',
        request_id="provider-request-raw-123",
        phase="gemini_cache_create",
    )
    observed = provider_http_error_observation(exc)
    wire = json.dumps(observed, ensure_ascii=False)
    assert observed["status"] == 503
    assert observed["phase"] == "gemini_cache_create"
    assert observed["request_id"].startswith("sha256:")
    assert "provider-request-raw-123" not in wire
    assert "topsecret" not in wire
    assert "AIza123456789012345678901234567890" not in wire
    assert "p@ss" not in wire
    assert "[redacted]" in wire


@pytest.mark.asyncio
async def test_stateful_manager_v2_measures_exact_prefix_before_resource_lookup():
    events = []

    class FakeAdapter:
        adapter_version = "fake-v2/1"

        def build_spec(self, **kwargs):
            assert kwargs.get("provider_physical_plan") == {"marker": "v2"}
            return {
                "cacheable": True,
                "content_fingerprint": "fp",
                "reuse_key": "reuse",
                "prefix_version": 0,
                "compatible_prefix_fingerprints": {"0": "fp"},
                "projection_version": "projector/2",
                "layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION,
                "physical_plan_hash": "plan-hash",
                "measurement_order": "before_lookup",
            }

        async def measure(self, *, spec):
            events.append("measure")
            return 2048

        async def get(self, *, handle, operation=None):
            events.append("get")
            return {"handle": handle, "expire_time": "2099-01-01T00:00:00Z"}

        async def create(self, **kwargs):
            raise AssertionError("ready resource should be reused")

    adapter = FakeAdapter()

    class Registry:
        def maybe_get(self, connection_id):
            return adapter

    class Repo:
        settings = settings()

        @staticmethod
        def cache_scope_hash(**kwargs):
            return "scope"

        async def find_compatible_cache_resources(self, **kwargs):
            events.append("find")
            return [{
                "id": "resource-v2",
                "generation": 1,
                "provider_handle_ref": "cachedContents/v2",
                "expire_time": "2099-01-01T00:00:00Z",
                "content_fingerprint": "fp",
                "prefix_version": 0,
                "token_count": 1024,
            }]

    result = await StatefulResourceManager(Repo(), Registry()).prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        plan={
            "requested_mode": "on",
            "profile_hash": "profile",
            "mechanism_config": {"minimum_cacheable_tokens": 1024},
        },
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=1),
        snapshot={"connection_id": "conn", "offering_id": "o", "model": "gemini-3.1-flash-lite"},
        session={"metadata": {"_relay_route": {"account_scope_hash": "a"}}},
        history=[],
        provider_physical_plan={"marker": "v2"},
    )
    assert result["decision_reason"] == "resource_reused"
    assert result["metadata"]["prefix_token_count"] == 2048
    assert events == ["measure", "find", "get"]


@pytest.mark.asyncio
async def test_cache_create_provider_http_error_is_sanitized_in_ledger_and_structured_log(caplog):
    recorded = []
    finished = []

    class FakeAdapter:
        adapter_version = "fake/1"

        def build_spec(self, **kwargs):
            return {
                "cacheable": True,
                "content_fingerprint": "fp",
                "reuse_key": "reuse",
                "prefix_version": 0,
                "compatible_prefix_fingerprints": {"0": "fp"},
                "projection_version": "projector/2",
                "measurement_order": "before_lookup",
            }

        async def measure(self, *, spec):
            return 4096

        async def create(self, *, spec, operation):
            raise ProviderHTTPError(
                503,
                b'{"message":"Bearer raw-secret-token","api_key":"AIza123456789012345678901234567890"}',
                request_id="raw-provider-request-id",
                phase="gemini_cache_create",
            )

    class Registry:
        def maybe_get(self, connection_id):
            return FakeAdapter()

    class Repo:
        settings = settings()

        @staticmethod
        def cache_scope_hash(**kwargs):
            return "scope"

        async def find_compatible_cache_resources(self, **kwargs):
            return []

        async def create_cache_operation_intent(self, **kwargs):
            return {"op_id": "op-http-error", "lease_owner": "worker", "lease_epoch": 2}

        async def start_cache_operation(self, **kwargs):
            return True

        async def record_cache_operation_observation(self, **kwargs):
            recorded.append(kwargs)
            return True

        async def finish_cache_operation(self, **kwargs):
            finished.append(kwargs)
            return True

    caplog.set_level(logging.INFO, logger="model-relay-cache")
    result = await StatefulResourceManager(Repo(), Registry()).prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        plan={
            "requested_mode": "auto",
            "allow_uncached_same_context": True,
            "profile_hash": "profile",
            "mechanism_config": {"minimum_cacheable_tokens": 1024},
        },
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=2),
        snapshot={"connection_id": "conn", "offering_id": "o", "model": "gemini-3.1-flash-lite"},
        session={"metadata": {"_relay_route": {"account_scope_hash": "a"}}},
        history=[],
    )
    assert result["mechanism"] is None
    assert result["decision_reason"] == "cache_create_unknown"
    assert recorded and finished and finished[-1]["state"] == "unknown"
    ledger_wire = json.dumps(recorded[-1], ensure_ascii=False)
    finish_wire = json.dumps(finished[-1], ensure_ascii=False)
    assert "raw-provider-request-id" not in ledger_wire + finish_wire
    assert "raw-secret-token" not in ledger_wire + finish_wire
    assert "AIza123456789012345678901234567890" not in ledger_wire + finish_wire
    assert '"status": 503' in ledger_wire or '"status":503' in ledger_wire
    assert "gemini_cache_create" in ledger_wire
    assert "cache_create_provider_http_error" in caplog.text
    assert "raw-provider-request-id" not in caplog.text
    assert "raw-secret-token" not in caplog.text


@pytest.mark.asyncio
async def test_missing_cache_handle_is_logged_unknown_and_never_verified_or_recreated(caplog):
    events = []
    recorded = []
    finished = []

    class FakeAdapter:
        adapter_version = "fake/1"

        def build_spec(self, **kwargs):
            return {
                "cacheable": True,
                "content_fingerprint": "fp",
                "reuse_key": "reuse",
                "prefix_version": 0,
                "compatible_prefix_fingerprints": {"0": "fp"},
                "projection_version": "projector/2",
                "measurement_order": "before_lookup",
            }

        async def measure(self, *, spec):
            events.append("measure")
            return 4096

        async def create(self, *, spec, operation):
            events.append("create")
            return {"handle": None, "provider_request_id": "raw-missing-handle-request-id"}

        async def get(self, *, handle, operation=None):
            events.append("get")
            raise AssertionError("missing handle must never be verified")

    class Registry:
        def maybe_get(self, connection_id):
            return FakeAdapter()

    class Repo:
        settings = settings()

        @staticmethod
        def cache_scope_hash(**kwargs):
            return "scope"

        async def find_compatible_cache_resources(self, **kwargs):
            events.append("find")
            return []

        async def create_cache_operation_intent(self, **kwargs):
            events.append("claim")
            return {"op_id": "op-missing", "lease_owner": "worker", "lease_epoch": 4}

        async def start_cache_operation(self, **kwargs):
            events.append("start")
            return True

        async def record_cache_operation_observation(self, **kwargs):
            events.append("record")
            recorded.append(kwargs)
            return True

        async def finish_cache_operation(self, **kwargs):
            events.append("finish")
            finished.append(kwargs)
            return True

        async def publish_cache_resource(self, **kwargs):
            raise AssertionError("missing handle must never be published")

    caplog.set_level(logging.INFO, logger="model-relay-cache")
    result = await StatefulResourceManager(Repo(), Registry()).prepare(
        request_row={"id": "r", "tenant_id": "t", "conversation_hash": "c", "session_id": "s"},
        plan={
            "requested_mode": "auto",
            "allow_uncached_same_context": True,
            "profile_hash": "profile",
            "mechanism_config": {"minimum_cacheable_tokens": 1024},
        },
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[],
        fence=ExecutionFence(owner="worker", epoch=4),
        snapshot={"connection_id": "conn", "offering_id": "o", "model": "gemini-3.1-flash-lite"},
        session={"metadata": {"_relay_route": {"account_scope_hash": "a"}}},
        history=[],
    )
    assert result["mechanism"] is None
    assert result["decision_reason"] == "cache_handle_unavailable"
    assert events == ["measure", "find", "claim", "start", "create", "record", "finish"]
    assert finished[-1]["state"] == "unknown"
    assert finished[-1]["raw_result"]["recreate_attempted"] is False
    assert "raw-missing-handle-request-id" not in json.dumps(recorded[-1])
    assert "cache_handle_unavailable_no_recreate" in caplog.text
    assert "raw-missing-handle-request-id" not in caplog.text


def test_gemini_4_primary_transport_is_files_api_even_for_small_materials():
    from app.materials.gemini_transport import decide_gemini_transport

    decision = decide_gemini_transport(
        actual_size=1024,
        request_file_total_bytes=1024,
        request_file_count=1,
        threshold_bytes=99 * 1024 * 1024,
    )
    assert decision.mode == "gemini_files"
    assert decision.source == "gemini_files_preferred"


def test_gemini_4_inline_fallback_planner_under_70mb_caches_all_failed_session_materials():
    from app.materials.gemini_cache_projection import plan_gemini_cache_materials

    mib = 1024 * 1024
    rows = [
        {"id": "m-a", "actual_size": 20 * mib},
        {"id": "m-b", "actual_size": 30 * mib},
    ]
    bindings = [
        {"material_id": "m-a", "representation": "gemini_external_url"},
        {"material_id": "m-b", "representation": "gemini_external_url"},
    ]
    plan = plan_gemini_cache_materials(
        material_rows=rows,
        material_bindings=bindings,
        session_material_ids=["m-a", "m-b"],
        inline_limit_bytes=70 * mib,
    )
    assert plan.total_material_bytes == 50 * mib
    assert plan.inline_material_ids == ("m-a", "m-b")
    assert plan.cache_material_ids == ("m-a", "m-b")
    assert plan.inference_only_session_material_ids == ()


def test_gemini_4_inline_fallback_planner_over_70mb_chooses_small_files_deterministically():
    from app.materials.gemini_cache_projection import plan_gemini_cache_materials

    mib = 1024 * 1024
    rows = [
        {"id": "m-large", "actual_size": 60 * mib},
        {"id": "m-small-b", "actual_size": 20 * mib},
        {"id": "m-small-a", "actual_size": 10 * mib},
    ]
    bindings = [
        {"material_id": "m-large", "representation": "gemini_external_url"},
        {"material_id": "m-small-b", "representation": "gemini_external_url"},
        {"material_id": "m-small-a", "representation": "gemini_external_url"},
    ]
    plan = plan_gemini_cache_materials(
        material_rows=rows,
        material_bindings=bindings,
        session_material_ids=["m-large", "m-small-b", "m-small-a"],
        inline_limit_bytes=70 * mib,
    )
    # Sorted by (size, id): 10 MiB + 20 MiB fit; adding 60 MiB would exceed the strict budget.
    assert plan.inline_material_ids == ("m-small-a", "m-small-b")
    assert plan.cache_material_ids == ("m-small-b", "m-small-a")
    assert plan.inference_only_session_material_ids == ("m-large",)


def test_gemini_4_hybrid_physical_plan_keeps_large_external_url_out_of_cache_and_in_inference():
    import base64

    session = _gemini_v2_session(material_manifest=["m-small", "m-large"])
    snapshot = {
        "schema_version": "relay-request/2.3",
        "model": "gemini-3.1-flash-lite",
        "instructions": "dynamic-stage",
        "input": "incremental instruction",
    }
    inference_bindings = [
        {
            "material_id": "m-small",
            "binding_generation": 1,
            "binding_kind": "gemini_external_url",
            "representation": "gemini_external_url",
            "connection_id": "aihubmix_gemini_native",
            "account_scope_hash": "acct",
            "provider": "gemini",
            "external_uri": "https://supabase.example/small",
            "content_sha256": "sha-small",
            "content_type": "application/pdf",
            "filename": "small.pdf",
        },
        {
            "material_id": "m-large",
            "binding_generation": 1,
            "binding_kind": "gemini_external_url",
            "representation": "gemini_external_url",
            "connection_id": "aihubmix_gemini_native",
            "account_scope_hash": "acct",
            "provider": "gemini",
            "external_uri": "https://supabase.example/large",
            "content_sha256": "sha-large",
            "content_type": "application/pdf",
            "filename": "large.pdf",
        },
    ]
    cache_projection = {
        "schema_version": "relay-gemini-cache-material-plan/1",
        "mode": "files_preferred_inline_small_subset",
        "total_material_bytes": 80 * 1024 * 1024,
        "inline_limit_bytes": 70 * 1024 * 1024,
        "cache_material_ids": ["m-small"],
        "inline_material_ids": ["m-small"],
        "inference_only_session_material_ids": ["m-large"],
        "cache_material_bindings": [
            {
                "material_id": "m-small",
                "binding_generation": 1,
                "binding_kind": "gemini_inline_data",
                "representation": "gemini_inline_data",
                "connection_id": "aihubmix_gemini_native",
                "account_scope_hash": "acct",
                "provider": "gemini",
                "content_sha256": "sha-small",
                "content_type": "application/pdf",
                "filename": "small.pdf",
                "inline_data": base64.b64encode(b"small-static-data").decode("ascii"),
            }
        ],
    }
    plan = build_gemini_physical_cache_plan(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-small", "m-large"],
        material_bindings=inference_bindings,
        context_plan={"context_plan_hash": "ctx"},
        cache_material_projection=cache_projection,
    )

    cached_wire = json.dumps(plan["cached_prefix"], ensure_ascii=False)
    suffix_wire = json.dumps(plan["uncached_suffix"], ensure_ascii=False)
    full_wire = json.dumps(plan["full_uncached_payload"], ensure_ascii=False)
    assert "inlineData" in cached_wire
    assert "https://supabase.example/small" not in cached_wire
    assert "https://supabase.example/large" not in cached_wire
    assert "https://supabase.example/large" in suffix_wire
    assert "https://supabase.example/small" not in suffix_wire
    assert "incremental instruction" in suffix_wire
    assert "https://supabase.example/small" in full_wire
    assert "https://supabase.example/large" in full_wire
    assert plan["dependencies"]["inference_only_session_material_ids"] == ["m-large"]


def test_gemini_4_exact_70mb_uses_large_aggregate_branch():
    from app.materials.gemini_cache_projection import plan_gemini_cache_materials

    mib = 1024 * 1024
    rows = [
        {"id": "m-small", "actual_size": 10 * mib},
        {"id": "m-large", "actual_size": 60 * mib},
    ]
    bindings = [
        {"material_id": "m-small", "representation": "gemini_external_url"},
        {"material_id": "m-large", "representation": "gemini_external_url"},
    ]
    plan = plan_gemini_cache_materials(
        material_rows=rows,
        material_bindings=bindings,
        session_material_ids=["m-small", "m-large"],
        inline_limit_bytes=70 * mib,
    )
    assert plan.total_material_bytes == 70 * mib
    assert plan.mode == "files_preferred_inline_small_subset"
    assert plan.inline_material_ids == ("m-small",)
    assert plan.inference_only_session_material_ids == ("m-large",)


class _FakeGenAIFileState:
    def __init__(self, name: str):
        self.name = name
        self.value = name


class _FakeGenAIFile:
    def __init__(self, *, name: str, uri: str, mime_type: str = "application/pdf", state: str = "ACTIVE"):
        self.name = name
        self.uri = uri
        self.mime_type = mime_type
        self.display_name = name.rsplit("/", 1)[-1]
        self.state = _FakeGenAIFileState(state)


class _FakePart:
    @classmethod
    def from_text(cls, *, text):
        return ("text", text)

    @classmethod
    def from_bytes(cls, *, data, mime_type):
        return ("bytes", data, mime_type)

    @classmethod
    def from_uri(cls, *, file_uri, mime_type):
        return ("uri", file_uri, mime_type)


class _FakeContent:
    def __init__(self, *, role, parts):
        self.role = role
        self.parts = parts


class _FakeUploadFileConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeCreateCachedContentConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeUpdateCachedContentConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeGenAITypes:
    UploadFileConfig = _FakeUploadFileConfig
    CreateCachedContentConfig = _FakeCreateCachedContentConfig
    UpdateCachedContentConfig = _FakeUpdateCachedContentConfig
    Part = _FakePart
    Content = _FakeContent


class _FakeFilesAPI:
    def __init__(self, uploaded, rehydrated=None):
        self.uploaded = uploaded
        self.rehydrated = rehydrated or uploaded
        self.get_calls = []
        self.upload_calls = []
        self.delete_calls = []

    async def upload(self, *, file, config):
        self.upload_calls.append((file, config))
        return self.uploaded

    async def get(self, *, name):
        self.get_calls.append(name)
        return self.rehydrated

    async def delete(self, *, name):
        self.delete_calls.append(name)
        return None


class _FakeCachesAPI:
    def __init__(self):
        self.create_calls = []

    async def create(self, *, model, config):
        self.create_calls.append((model, config))
        return type(
            "FakeCache",
            (),
            {
                "name": "cachedContents/sdk-cache-1",
                "expire_time": "2099-01-01T00:00:00Z",
                "create_time": "2026-10-03T00:00:00Z",
                "usage_metadata": {"totalTokenCount": 5000},
            },
        )()


class _FakeAIO:
    def __init__(self, files_api, caches_api):
        self.files = files_api
        self.caches = caches_api

    async def aclose(self):
        return None


class _FakeGenAIClient:
    def __init__(self, uploaded, rehydrated=None):
        self.files_api = _FakeFilesAPI(uploaded, rehydrated=rehydrated)
        self.caches_api = _FakeCachesAPI()
        self.aio = _FakeAIO(self.files_api, self.caches_api)

    def close(self):
        return None


@pytest.mark.asyncio
async def test_layout5_google_genai_passes_uploaded_file_object_directly_to_cache_create():
    from app.materials.provider_files.base import MaterialFile
    from app.materials.provider_files.gemini_aihubmix import GeminiAIHubMixFileAdapter
    from app.providers.gemini_genai_sdk import GeminiAIHubMixGenAIClient

    uploaded = _FakeGenAIFile(
        name="files/session-pdf",
        uri="https://files.example/session-pdf",
    )
    fake_client = _FakeGenAIClient(uploaded)
    sdk = GeminiAIHubMixGenAIClient(
        settings(),
        client=fake_client,
        types_module=_FakeGenAITypes,
    )
    file_adapter = GeminiAIHubMixFileAdapter(settings(), sdk=sdk)
    prepared = await file_adapter.prepare(
        MaterialFile(
            material_id="m-session",
            tenant_id="t",
            conversation_hash="c",
            filename="session.pdf",
            content_type="application/pdf",
            size_bytes=8,
            sha256="sha-session",
            data=b"pdf-data",
        ),
        generation=1,
    )
    persisted_binding = {
        "material_id": "m-session",
        "binding_generation": 1,
        "binding_kind": prepared.binding["representation"],
        "content_sha256": "sha-session",
        "content_type": "application/pdf",
        "filename": "session.pdf",
        **prepared.binding,
    }
    session = _gemini_v2_session(material_manifest=["m-session"])
    snapshot = {
        "schema_version": "relay-request/2.3",
        "model": "gemini-3.1-flash-lite",
        "instructions": "dynamic",
        "input": "question",
        "protocol_profile_hash": "profile",
        "capability_contract_hash": "cap",
        "cache_contract_hash": "cache",
    }
    physical = build_gemini_physical_cache_plan(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-session"],
        material_bindings=[persisted_binding],
        context_plan={"context_plan_hash": "ctx"},
    )
    cache_adapter = GeminiAIHubMixCacheResourceAdapter(settings(), sdk=sdk)
    spec = cache_adapter.build_spec(
        snapshot=snapshot,
        history=[],
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[persisted_binding],
        session=session,
        ttl_seconds=300,
        provider_physical_plan=physical,
    )
    assert spec["cache_create_transport"] == "google_genai_sdk"
    created = await cache_adapter.create(spec=spec, operation={"lease_epoch": 1})
    assert created["handle"] == "cachedContents/sdk-cache-1"
    assert fake_client.files_api.get_calls == []
    _, config = fake_client.caches_api.create_calls[-1]
    assert config.ttl == "300s"
    # The exact object returned by files.upload survives through the shared SDK
    # wrapper and is handed to CreateCachedContentConfig.contents.
    assert config.contents[0][0] is uploaded


@pytest.mark.asyncio
async def test_layout5_google_genai_rehydrates_file_object_after_process_boundary():
    from app.providers.gemini_genai_sdk import GeminiAIHubMixGenAIClient

    rehydrated = _FakeGenAIFile(
        name="files/session-pdf",
        uri="https://files.example/session-pdf",
    )
    fake_client = _FakeGenAIClient(rehydrated, rehydrated=rehydrated)
    sdk = GeminiAIHubMixGenAIClient(
        settings(),
        client=fake_client,
        types_module=_FakeGenAITypes,
    )
    cache_adapter = GeminiAIHubMixCacheResourceAdapter(settings(), sdk=sdk)
    binding = {
        "material_id": "m-session",
        "binding_generation": 1,
        "binding_kind": "gemini_file_uri",
        "representation": "gemini_file_uri",
        "connection_id": "aihubmix_gemini_native",
        "external_uri": rehydrated.uri,
        "file_uri": rehydrated.uri,
        "external_file_id": rehydrated.name,
        "provider_file_id": rehydrated.name,
        "content_sha256": "sha-session",
        "content_type": "application/pdf",
        "metadata": {"mime_type": "application/pdf"},
    }
    session = _gemini_v2_session(material_manifest=["m-session"])
    snapshot = {
        "schema_version": "relay-request/2.3",
        "model": "gemini-3.1-flash-lite",
        "instructions": "dynamic",
        "input": "question",
        "protocol_profile_hash": "profile",
        "capability_contract_hash": "cap",
        "cache_contract_hash": "cache",
    }
    physical = build_gemini_physical_cache_plan(
        snapshot=snapshot,
        session=session,
        history=[],
        material_ids=["m-session"],
        material_bindings=[binding],
        context_plan={"context_plan_hash": "ctx"},
    )
    spec = cache_adapter.build_spec(
        snapshot=snapshot,
        history=[],
        context_plan={"context_plan_hash": "ctx"},
        material_bindings=[binding],
        session=session,
        ttl_seconds=300,
        provider_physical_plan=physical,
    )
    await cache_adapter.create(spec=spec, operation={"lease_epoch": 1})
    assert fake_client.files_api.get_calls == ["files/session-pdf"]
    _, config = fake_client.caches_api.create_calls[-1]
    assert config.contents[0][0] is rehydrated
