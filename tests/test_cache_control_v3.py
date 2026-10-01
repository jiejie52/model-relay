from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import json

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
from app.providers.base import ProviderHTTPError
from app.providers.openai_compatible import OpenAICompatibleResponsesProvider
from app.providers.gemini_native import GeminiNativeAdapter
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
