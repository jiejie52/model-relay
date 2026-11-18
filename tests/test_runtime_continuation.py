"""Unit and integration verification for Wave 2 Runtime Continuation:
- ContinuationBinder (valid binding, fallback degradation, expiry, status, scope checks)
- AtomicDispatchSealer integration (continuation sealing, lock dispatch, snapshot retention, integrity)
- RelayRuntimeEngine flow integration (failure stage tracking, binder invocation)
- TransportGuard continuation interaction path protection
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from app.core.idempotency import stable_hash
from app.execution_plane.continuation_contracts import ContinuationIntentPlan
from app.protocol_adapters.base import ProviderRequestError
from app.runtime.continuation_binder import ContinuationBinder
from app.runtime.continuation_contracts import ContinuationExecutionBinding
from app.runtime.contracts import RuntimeState, SealedDispatchBundle
from app.runtime.dispatch_sealer import AtomicDispatchSealer
from app.runtime.engine import RelayRuntimeEngine
from app.runtime.transport_guard import ExecutionFencedAsyncTransport
from app.shared_contracts import ExecutionConflictError


class MockFence:
    def __init__(self, owner="worker-1", epoch=42):
        self.owner = owner
        self.epoch = epoch

    def canonical(self):
        return {"fence_owner": self.owner, "fence_epoch": self.epoch}


def _make_dummy_state(*, parent_anchor_id=None, continuation_mode="auto", parent_version=1):
    session = {
        "id": "sess-test-001",
        "context_policy": "conversation",
        "metadata": {"_relay_route": {"connection_identity_hash": "ident-1"}},
    }
    request_row = {
        "id": "req-test-001",
        "session_id": "sess-test-001",
        "tenant_id": "tenant-test",
        "conversation_hash": "conv-hash-1",
        "provider_dispatch_state": "not_sent",
    }
    plan_obj = ContinuationIntentPlan(
        continuation_mode=continuation_mode,
        parent_anchor_id=parent_anchor_id,
        parent_history_version=parent_version,
    )
    snapshot = {
        "connection_identity_hash": "ident-1",
        "connection_id": "conn-1",
        "continuation_intent_plan": plan_obj.to_dict(),
        "historical_material_safety_seconds": 30,
        "schema_version": "relay-request/2.5",
    }
    history = [
        {"turn": 0, "transport_history": {"role": "user", "text": "hello"}},
        {"turn": 1, "transport_history": {"role": "model", "text": "hi"}},
        {"turn": 2, "transport_history": {"role": "user", "text": "next question"}},
    ]

    mock_plan = MagicMock()
    mock_plan.request_record = request_row
    mock_plan.session_record = session
    mock_plan.snapshot = snapshot
    mock_plan.history = history
    mock_plan.continuation_intent_plan = plan_obj
    mock_plan.execution_fence = MockFence()
    mock_plan.connection_spec_hash = ""
    mock_plan.account_scope_id = ""

    state = RuntimeState.from_plan(mock_plan, 1000.0)
    return state


class TestContinuationBinder(unittest.IsolatedAsyncioTestCase):
    async def test_binder_mode_off_or_missing_anchor(self):
        repo = MagicMock()
        repo.get_continuation_anchor = AsyncMock()
        binder = ContinuationBinder(repo)

        # 1. continuation_mode == 'off'
        state_off = _make_dummy_state(parent_anchor_id="anchor-123", continuation_mode="off")
        binding = await binder.bind_for_context(state_off)
        self.assertEqual(binding.mechanism, "none")
        self.assertIsNone(binding.active_anchor_id)
        self.assertFalse(binding.fallback_occurred)
        self.assertEqual(state_off.continuation_binding, binding)
        self.assertIsNone(state_off.continuation_anchor)
        repo.get_continuation_anchor.assert_not_called()

        # 2. parent_anchor_id is None
        state_no_anchor = _make_dummy_state(parent_anchor_id=None, continuation_mode="auto")
        binding2 = await binder.bind_for_context(state_no_anchor)
        self.assertEqual(binding2.mechanism, "none")
        self.assertIsNone(binding2.active_anchor_id)
        self.assertFalse(binding2.fallback_occurred)
        repo.get_continuation_anchor.assert_not_called()

    async def test_binder_valid_anchor(self):
        repo = MagicMock()
        now = datetime.now(timezone.utc)
        future = (now + timedelta(hours=1)).isoformat()
        repo.get_continuation_anchor = AsyncMock(return_value={
            "id": "anc-999",
            "state": "active",
            "session_id": "sess-test-001",
            "tenant_id": "tenant-test",
            "history_version": 2,
            "upstream_handle": {"interaction_id": "interaction-abc-123"},
            "expires_at": future,
            "mechanism": "upstream_handle",
        })

        binder = ContinuationBinder(repo)
        state = _make_dummy_state(parent_anchor_id="anc-999", continuation_mode="auto", parent_version=2)
        binding = await binder.bind_for_context(state)

        self.assertEqual(binding.mechanism, "upstream_handle")
        self.assertEqual(binding.active_anchor_id, "anc-999")
        self.assertFalse(binding.fallback_occurred)
        self.assertIsNone(binding.fallback_reason)
        self.assertEqual(binding.upstream_handle, {"interaction_id": "interaction-abc-123"})
        # Original history had 3 turns (0, 1, 2). covered_history_version=2 -> delta turns has 1 turn (index 2)
        self.assertEqual(len(binding.bound_delta_turns), 1)
        self.assertEqual(binding.bound_delta_turns[0]["turn"], 2)
        self.assertIsNotNone(state.continuation_anchor)
        self.assertEqual(state.snapshot["continuation_binding"]["active_anchor_id"], "anc-999")

    async def test_binder_fallback_when_anchor_expired(self):
        repo = MagicMock()
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        repo.get_continuation_anchor = AsyncMock(return_value={
            "id": "anc-expired",
            "state": "active",
            "session_id": "sess-test-001",
            "tenant_id": "tenant-test",
            "history_version": 2,
            "upstream_handle": {"handle": "xyz"},
            "expires_at": past,
        })

        binder = ContinuationBinder(repo)
        state = _make_dummy_state(parent_anchor_id="anc-expired")
        binding = await binder.bind_for_context(state)

        self.assertTrue(binding.fallback_occurred)
        self.assertEqual(binding.fallback_reason, "ANCHOR_EXPIRED_OR_INVALID")
        self.assertEqual(binding.mechanism, "none")
        self.assertIsNone(binding.active_anchor_id)
        self.assertIsNone(state.continuation_anchor)

    async def test_binder_fallback_when_within_safety_margin(self):
        repo = MagicMock()
        almost_expired = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
        repo.get_continuation_anchor = AsyncMock(return_value={
            "id": "anc-margin",
            "state": "active",
            "session_id": "sess-test-001",
            "tenant_id": "tenant-test",
            "history_version": 1,
            "expires_at": almost_expired,
        })

        binder = ContinuationBinder(repo)
        state = _make_dummy_state(parent_anchor_id="anc-margin")
        state.snapshot["continuation_safety_seconds"] = 30
        binding = await binder.bind_for_context(state)

        self.assertTrue(binding.fallback_occurred)
        self.assertEqual(binding.fallback_reason, "ANCHOR_EXPIRED_OR_INVALID")

    async def test_binder_fallback_when_anchor_inactive_or_not_found(self):
        repo = MagicMock()
        binder = ContinuationBinder(repo)

        # 1. Anchor not found
        repo.get_continuation_anchor = AsyncMock(return_value=None)
        state_none = _make_dummy_state(parent_anchor_id="anc-missing")
        binding1 = await binder.bind_for_context(state_none)
        self.assertTrue(binding1.fallback_occurred)
        self.assertEqual(binding1.fallback_reason, "ANCHOR_EXPIRED_OR_INVALID")

        # 2. Anchor state inactive / revoked
        repo.get_continuation_anchor = AsyncMock(return_value={
            "id": "anc-inactive",
            "state": "revoked",
            "session_id": "sess-test-001",
            "tenant_id": "tenant-test",
        })
        state_inactive = _make_dummy_state(parent_anchor_id="anc-inactive")
        binding2 = await binder.bind_for_context(state_inactive)
        self.assertTrue(binding2.fallback_occurred)
        self.assertEqual(binding2.fallback_reason, "ANCHOR_EXPIRED_OR_INVALID")

        # 3. Scope mismatch (tenant or session)
        repo.get_continuation_anchor = AsyncMock(return_value={
            "id": "anc-wrong-tenant",
            "state": "active",
            "session_id": "sess-test-001",
            "tenant_id": "tenant-DIFFERENT",
        })
        state_mismatch = _make_dummy_state(parent_anchor_id="anc-wrong-tenant")
        binding3 = await binder.bind_for_context(state_mismatch)
        self.assertTrue(binding3.fallback_occurred)


class TestAtomicDispatchSealer(unittest.IsolatedAsyncioTestCase):
    async def test_sealer_active_continuation(self):
        repo = MagicMock()
        repo.seal_continuation_and_dispatch_v90 = AsyncMock(return_value=True)

        sealer = AtomicDispatchSealer(repo)
        state = _make_dummy_state(parent_anchor_id="anc-123")
        state.continuation_binding = ContinuationExecutionBinding(
            mechanism="upstream_handle",
            active_anchor_id="anc-123",
            upstream_handle={"cachedContent": "cachedContents/123"},
            bound_delta_turns=[{"turn": 2}],
            fallback_occurred=False,
        )

        bundle = await sealer.seal_before_dispatch(state)
        self.assertIsInstance(bundle, SealedDispatchBundle)
        self.assertIsNotNone(bundle.continuation_execution_binding)
        self.assertEqual(bundle.continuation_execution_binding["active_anchor_id"], "anc-123")

        # Check that seal_continuation_and_dispatch_v90 was called
        repo.seal_continuation_and_dispatch_v90.assert_awaited_once_with(
            request_id=state.request_id,
            anchor_id="anc-123",
            fence_owner=state.fence.owner,
            fence_epoch=state.fence.epoch,
            hold_seconds=300,
        )

        # Check snapshot retention
        self.assertEqual(state.execution_snapshot["_relay_continuation_execution"], bundle.continuation_execution_binding)
        self.assertEqual(state.snapshot["_relay_continuation_execution"], bundle.continuation_execution_binding)

        # Integrity verification
        self.assertTrue(bundle.verify_integrity())

    async def test_sealer_fallback_continuation(self):
        repo = MagicMock()
        repo.seal_continuation_and_dispatch_v90 = AsyncMock()
        repo.mark_request_dispatch = AsyncMock(return_value=True)

        sealer = AtomicDispatchSealer(repo)
        state = _make_dummy_state(parent_anchor_id="anc-fallback")
        state.continuation_binding = ContinuationExecutionBinding(
            mechanism="none",
            active_anchor_id=None,
            fallback_occurred=True,
            fallback_reason="ANCHOR_EXPIRED_OR_INVALID",
        )

        bundle = await sealer.seal_before_dispatch(state)
        # When fallback occurs, seal_continuation_and_dispatch_v90 should NOT be called;
        # it must proceed through normal dispatch
        repo.seal_continuation_and_dispatch_v90.assert_not_called()
        repo.mark_request_dispatch.assert_awaited_once()

        self.assertTrue(bundle.continuation_execution_binding["fallback_occurred"])
        self.assertTrue(bundle.verify_integrity())

    async def test_sealer_continuation_seal_rejected(self):
        repo = MagicMock()
        repo.seal_continuation_and_dispatch_v90 = AsyncMock(return_value=False)

        sealer = AtomicDispatchSealer(repo)
        state = _make_dummy_state(parent_anchor_id="anc-rejected")
        state.continuation_binding = ContinuationExecutionBinding(
            mechanism="upstream_handle",
            active_anchor_id="anc-rejected",
            fallback_occurred=False,
        )

        from app.runtime.execution_guard import ExecutionLeaseLost
        with self.assertRaises(ExecutionLeaseLost):
            await sealer.seal_before_dispatch(state)

    async def test_sealer_integrity_conflict_detected(self):
        repo = MagicMock()
        repo.seal_continuation_and_dispatch_v90 = AsyncMock(return_value=True)

        sealer = AtomicDispatchSealer(repo)
        state = _make_dummy_state(parent_anchor_id="anc-int")
        state.continuation_binding = ContinuationExecutionBinding(
            mechanism="upstream_handle",
            active_anchor_id="anc-int",
            fallback_occurred=False,
        )

        bundle = await sealer.seal_before_dispatch(state)

        # Tampering with snapshot continuation execution binding must trigger ExecutionConflictError
        tampered_snapshot = dict(bundle.snapshot)
        tampered_snapshot["_relay_continuation_execution"] = {"tampered": True}
        tampered_bundle = SealedDispatchBundle(
            snapshot=tampered_snapshot,
            session=bundle.session,
            history=bundle.history,
            material_ids=bundle.material_ids,
            material_bindings=bundle.material_bindings,
            tenant_id=bundle.tenant_id,
            conversation_hash=bundle.conversation_hash,
            request_id=bundle.request_id,
            session_id=bundle.session_id,
            context_plan=bundle.context_plan,
            cache_execution_binding=bundle.cache_execution_binding,
            continuation_execution_binding=bundle.continuation_execution_binding,
            execution_fence=bundle.execution_fence,
            provider_physical_plan=bundle.provider_physical_plan,
            affinity_headers=bundle.affinity_headers,
            seal_id=bundle.seal_id,
            seal_hash=bundle.seal_hash,
        )
        with self.assertRaises(ExecutionConflictError):
            tampered_bundle.verify_integrity()


class TestRelayRuntimeEngine(unittest.IsolatedAsyncioTestCase):
    async def test_engine_continuation_bind_in_flow(self):
        repo = MagicMock()
        repo.get_request = AsyncMock(return_value={"provider_dispatch_state": "not_sent", "tenant_id": "tenant-test"})
        storage = MagicMock()
        providers = MagicMock()
        materials = MagicMock()
        bindings = MagicMock()
        bindings.bind_for_plan = AsyncMock()
        settings = MagicMock()

        mock_continuation_binder = MagicMock()
        mock_continuation_binder.bind_for_context = AsyncMock()

        engine = RelayRuntimeEngine(
            repo, storage, providers, materials, bindings, settings,
            continuation_binder=mock_continuation_binder,
        )

        state = _make_dummy_state(parent_anchor_id="anc-flow")
        mock_plan = state.plan

        engine.verifier.verify = AsyncMock()
        engine.projector.prepare_scope = AsyncMock()
        engine.projector.project = AsyncMock()
        engine.sealer.seal_before_dispatch = AsyncMock()
        engine.invoker.invoke_once_and_record = AsyncMock(return_value={"ok": True})

        result = await engine._execute(mock_plan)
        self.assertEqual(result, {"ok": True})
        mock_continuation_binder.bind_for_context.assert_awaited_once()

    async def test_engine_records_failure_stage_on_binder_error(self):
        repo = MagicMock()
        repo.get_request = AsyncMock(return_value={"provider_dispatch_state": "not_sent", "tenant_id": "tenant-test", "id": "req-test-001"})
        storage = MagicMock()
        providers = MagicMock()
        materials = MagicMock()
        bindings = MagicMock()
        settings = MagicMock()

        mock_continuation_binder = MagicMock()
        mock_continuation_binder.bind_for_context = AsyncMock(side_effect=RuntimeError("bind failure test"))

        engine = RelayRuntimeEngine(
            repo, storage, providers, materials, bindings, settings,
            continuation_binder=mock_continuation_binder,
        )
        engine.verifier.verify = AsyncMock()
        engine.projector.prepare_scope = AsyncMock()
        engine.errors.record = AsyncMock(return_value={"recorded": True})

        state = _make_dummy_state(parent_anchor_id="anc-err")
        fact = await engine._execute(state.plan)

        self.assertEqual(fact.failure_stage, "continuation_bind")
        engine.errors.record.assert_awaited_once()


class TestTransportGuard(unittest.IsolatedAsyncioTestCase):
    async def test_transport_guard_interaction_path(self):
        wrapped = MagicMock()
        wrapped.handle_async_request = AsyncMock(return_value=httpx.Response(200, json={"status": "ok"}))

        connection = MagicMock()
        connection.allowed_hosts = MagicMock(return_value=("api.deepseek.com", "generativelanguage.googleapis.com"))
        connection.spec.upload_base_url = None

        guard_transport = ExecutionFencedAsyncTransport(wrapped, connection=connection)

        # 1. Allowed host with continuation interaction path
        req_valid = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/interactions")
        resp = await guard_transport.handle_async_request(req_valid)
        self.assertEqual(resp.status_code, 200)

        # 2. Third-party continuation handle path on allowed host
        req_third_party = httpx.Request("POST", "https://api.deepseek.com/v1/continuation_handle_123")
        resp2 = await guard_transport.handle_async_request(req_third_party)
        self.assertEqual(resp2.status_code, 200)

        # 3. Forbidden host must raise CONNECTION_HOST_FORBIDDEN
        req_forbidden = httpx.Request("POST", "https://malicious.example.com/v1beta/interactions")
        with self.assertRaises(ValueError):
            await guard_transport.handle_async_request(req_forbidden)


if __name__ == "__main__":
    unittest.main()
