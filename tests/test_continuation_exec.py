"""Unit verification for continuation planning in RequestAcceptor, anchor commit in ResultCommitter,
restoration in ExecutionPlanLoader, and router diagnostic envelope serialization.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from app.api_v2.router import _request_envelope
from app.core.continuation_fingerprint import build_parent_head_fingerprint
from app.execution_plane.continuation_contracts import ContinuationIntentPlan
from app.execution_plane.contracts import FrozenExecutionPlan
from app.execution_plane.plan_loader import ExecutionPlanLoader
from app.execution_plane.request_acceptor import RequestAcceptor
from app.execution_plane.result_committer import ResultCommitter
from app.persistence.object_storage import ObjectLocation
from app.protocol_adapters.v2_base import StandardizedProviderResult
from app.runtime.continuation_contracts import ContinuationAnchorRecord
from app.runtime.contracts import ActualExecutionFact
from app.shared_contracts import ExecutionConflictError, ExecutionFence
from app.v2_models import ContinuationSummary, RequestEnvelope, SessionRequestCreate
from app.v2_repository import RelayV2Repository


class TestRouterContinuationEnvelope(unittest.TestCase):
    def test_envelope_from_compact_result(self):
        req_id = uuid4()
        sess_id = uuid4()
        row = {
            "id": str(req_id),
            "session_id": str(sess_id),
            "status": "succeeded",
            "execution_mode": "sync",
            "completed_history_version": 3,
            "compact_result": {
                "request_id": str(req_id),
                "status": "succeeded",
                "text": "Hello world",
                "_relay_continuation": {
                    "continuation_mode": "auto",
                    "mechanism": "stateful_anchor",
                    "active_anchor_id": "anc-12345",
                    "parent_anchor_id": "anc-parent",
                    "bound_turns_count": 3,
                    "fallback_occurred": False,
                    "plan_hash": "plan-hash-111",
                },
            },
        }
        envelope = _request_envelope(row)
        self.assertIsInstance(envelope, RequestEnvelope)
        self.assertIsNotNone(envelope.continuation)
        self.assertIsInstance(envelope.continuation, ContinuationSummary)
        self.assertEqual(envelope.continuation.continuation_mode, "auto")
        self.assertEqual(envelope.continuation.mechanism, "stateful_anchor")
        self.assertEqual(envelope.continuation.active_anchor_id, "anc-12345")
        self.assertEqual(envelope.continuation.parent_anchor_id, "anc-parent")
        self.assertEqual(envelope.continuation.bound_turns_count, 3)
        self.assertFalse(envelope.continuation.fallback_occurred)
        self.assertEqual(envelope.continuation.plan_hash, "plan-hash-111")

        # Serializes correctly to JSON dict
        dumped = envelope.model_dump(mode="json")
        self.assertIn("continuation", dumped)
        self.assertEqual(dumped["continuation"]["active_anchor_id"], "anc-12345")
        self.assertEqual(dumped["continuation"]["schema_version"], "relay-continuation-summary/1")

    def test_envelope_from_metadata(self):
        req_id = uuid4()
        sess_id = uuid4()
        row = {
            "id": str(req_id),
            "session_id": str(sess_id),
            "status": "queued",
            "execution_mode": "async",
            "expected_history_version": 1,
            "metadata": {
                "_relay_continuation": {
                    "continuation_mode": "auto",
                    "parent_anchor_id": "anc-parent-888",
                    "plan_hash": "plan-hash-222",
                }
            },
        }
        envelope = _request_envelope(row)
        self.assertIsNotNone(envelope.continuation)
        self.assertEqual(envelope.continuation.continuation_mode, "auto")
        self.assertEqual(envelope.continuation.parent_anchor_id, "anc-parent-888")
        self.assertEqual(envelope.continuation.plan_hash, "plan-hash-222")

    def test_envelope_without_continuation(self):
        req_id = uuid4()
        sess_id = uuid4()
        row = {
            "id": str(req_id),
            "session_id": str(sess_id),
            "status": "succeeded",
            "execution_mode": "sync",
            "compact_result": {"text": "Just plain text"},
        }
        envelope = _request_envelope(row)
        self.assertIsNone(envelope.continuation)


class TestPlanLoaderContinuation(unittest.TestCase):
    def setUp(self):
        self.repo = MagicMock()
        self.storage = MagicMock()
        self.settings = MagicMock()
        self.settings.execution_pool = "default"
        self.loader = ExecutionPlanLoader(self.repo, self.storage, self.settings)

    def test_load_restores_continuation_intent_plan(self):
        plan_obj = ContinuationIntentPlan(
            continuation_mode="auto",
            parent_anchor_id="anc-999",
            parent_history_version=2,
            parent_fingerprint="fp-parent",
        )
        plan_dict = plan_obj.to_dict()

        snapshot = {
            "schema_version": "relay-request/2.4",
            "session_id": "sess-1",
            "tenant_id": "tenant-1",
            "conversation_hash": "conv-1",
            "input": "test prompt",
            "provider": "google",
            "model": "gemini-2.5-flash",
            "options": {},
            "effective_options": {},
            "execution_authorization": {},
            "continuation_intent_plan": plan_dict,
        }

        row = {
            "id": "req-1",
            "session_id": "sess-1",
            "tenant_id": "tenant-1",
            "conversation_hash": "conv-1",
            "request_object_id": "obj-req-1",
            "expected_history_version": 0,
        }

        session = {
            "id": "sess-1",
            "history_version": 0,
            "context_policy": "conversation",
            "metadata": {},
        }

        self.repo.get_session = AsyncMock(return_value=session)
        self.loader.artifacts.load_json = AsyncMock(return_value=snapshot)
        fence = ExecutionFence(owner="worker-1", epoch=1)

        frozen_plan = asyncio.run(self.loader.load(row, fence))
        self.assertIsInstance(frozen_plan, FrozenExecutionPlan)
        self.assertIsNotNone(frozen_plan.continuation_intent_plan)
        self.assertEqual(frozen_plan.continuation_intent_plan["parent_anchor_id"], "anc-999")
        self.assertEqual(frozen_plan.continuation_intent_plan["continuation_mode"], "auto")
        self.assertEqual(frozen_plan.continuation_intent_plan["plan_hash"], plan_obj.plan_hash)

    def test_load_rejects_tampered_continuation_intent_plan(self):
        plan_obj = ContinuationIntentPlan(
            continuation_mode="auto",
            parent_anchor_id="anc-999",
            parent_history_version=2,
        )
        plan_dict = plan_obj.to_dict()
        # Tamper with the plan without updating plan_hash
        plan_dict["parent_anchor_id"] = "anc-tampered"

        snapshot = {
            "schema_version": "relay-request/2.4",
            "session_id": "sess-1",
            "tenant_id": "tenant-1",
            "conversation_hash": "conv-1",
            "input": "test prompt",
            "provider": "google",
            "model": "gemini-2.5-flash",
            "options": {},
            "effective_options": {},
            "execution_authorization": {},
            "continuation_intent_plan": plan_dict,
        }

        row = {
            "id": "req-1",
            "session_id": "sess-1",
            "tenant_id": "tenant-1",
            "conversation_hash": "conv-1",
            "request_object_id": "obj-req-1",
            "expected_history_version": 0,
        }

        session = {
            "id": "sess-1",
            "history_version": 0,
            "context_policy": "conversation",
            "metadata": {},
        }

        self.repo.get_session = AsyncMock(return_value=session)
        self.loader.artifacts.load_json = AsyncMock(return_value=snapshot)
        fence = ExecutionFence(owner="worker-1", epoch=1)

        with self.assertRaises(ExecutionConflictError):
            asyncio.run(self.loader.load(row, fence))


class TestResultCommitterAnchorAssembly(unittest.TestCase):
    def setUp(self):
        self.repo = MagicMock()
        self.storage = MagicMock()
        self.settings = MagicMock()
        self.settings.relay_result_soft_limit_bytes = 1000000
        self.settings.relay_result_hard_limit_bytes = 2000000
        self.settings.relay_result_preview_bytes = 10000
        self.committer = ResultCommitter(self.repo, self.storage, self.settings)

    def test_finalize_computes_and_passes_continuation_anchor(self):
        history = [
            {
                "transport_history": {
                    "user_message": {"role": "user", "content": "turn 0 question"},
                    "assistant_message": {"role": "assistant", "content": "turn 0 answer"},
                }
            }
        ]
        history_entry_1 = {
            "transport_history": {
                "user_message": {"role": "user", "content": "turn 1 question"},
                "assistant_message": {"role": "assistant", "content": "turn 1 answer"},
            }
        }

        continuation_intent = {
            "continuation_mode": "auto",
            "parent_anchor_id": "anc-parent-001",
            "plan_hash": "plan-hash-abc",
        }

        snapshot = {
            "schema_version": "relay-request/2.4",
            "provider": "google",
            "model": "gemini-2.5-flash",
            "connection_id": "conn-g-1",
            "account_scope_hash": "scope-hash-1",
            "observed_model_policy": "ignore",
            "continuation_intent_plan": continuation_intent,
        }

        plan = MagicMock()
        plan.request_record = {
            "id": "req-commit-1",
            "session_id": "sess-commit-1",
            "tenant_id": "tenant-commit-1",
            "conversation_hash": "conv-commit-1",
            "expected_history_version": 1,
            "request_identity_version": "relay-request/2.4",
        }
        plan.session_record = {
            "id": "sess-commit-1",
            "context_policy": "conversation",
            "history_mode": "cumulative",
            "history_version": 1,
        }
        plan.snapshot = snapshot
        plan.history = history
        plan.execution_fence = ExecutionFence(owner="worker-commit", epoch=1)

        result = StandardizedProviderResult(
            raw_bytes=b"{}",
            raw_json={},
            text="turn 1 answer",
            response_id="resp-provider-1",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            cached_tokens=0,
            response_output=None,
            history_entry=history_entry_1,
            observed={"upstream_handle": "cachedContents/new-anchor-handle-002"},
        )

        fact = ActualExecutionFact(
            request_id="req-commit-1",
            provider_dispatch_state="result_stored",
            standardized_result=result,
            provisional_raw_object_id="obj-raw-1",
            provisional_output_object_id="obj-out-1",
            execution_observation={"recorded_at": "2026-11-17T12:00:00Z"},
        )

        self.committer.artifacts.store_history = AsyncMock(return_value="obj-hist-2")
        self.repo.complete_request_v3 = AsyncMock(return_value=True)

        compact = asyncio.run(self.committer.finalize(plan, fact))

        # 1. Assert complete_request_v3 was called with continuation_anchor
        self.repo.complete_request_v3.assert_awaited_once()
        call_kwargs = self.repo.complete_request_v3.call_args.kwargs
        self.assertIn("continuation_anchor", call_kwargs)
        anchor = call_kwargs["continuation_anchor"]

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["session_id"], "sess-commit-1")
        self.assertEqual(anchor["history_version"], 2)
        self.assertEqual(anchor["parent_anchor_id"], "anc-parent-001")
        self.assertEqual(anchor["upstream_handle"], "cachedContents/new-anchor-handle-002")
        self.assertEqual(anchor["provider"], "google")
        self.assertEqual(anchor["connection_id"], "conn-g-1")
        self.assertEqual(anchor["state"], "active")

        # Head fingerprint verified against 2-turn history
        expected_fp = build_parent_head_fingerprint(history + [history_entry_1])
        self.assertEqual(anchor["fingerprint"], expected_fp)

        # 2. Assert compact_result contains diagnostic continuation summary
        self.assertIn("_relay_continuation", compact)
        diag = compact["_relay_continuation"]
        self.assertEqual(diag["continuation_mode"], "auto")
        self.assertEqual(diag["active_anchor_id"], anchor["anchor_id"])
        self.assertEqual(diag["parent_anchor_id"], "anc-parent-001")
        self.assertEqual(diag["bound_turns_count"], 2)
        self.assertFalse(diag["fallback_occurred"])

    def test_repository_routes_continuation_anchor_to_v90(self):
        backend = MagicMock()
        backend.rpc = AsyncMock(return_value=True)
        repo = RelayV2Repository(backend, settings=self.settings)

        anchor_data = {
            "anchor_id": "anc-test-123",
            "session_id": "sess-test",
            "history_version": 2,
            "fingerprint": "fp-test",
        }

        # Calling complete_request_v3 with continuation_anchor should trigger complete_continuation_request_v90
        ok = asyncio.run(
            repo.complete_request_v3(
                request_id=UUID("00000000-0000-0000-0000-000000000001"),
                session_id=UUID("00000000-0000-0000-0000-000000000002"),
                expected_history_version=1,
                history_object_id="hist-obj",
                result_object_id="res-obj",
                output_object_id="out-obj",
                compact_result={"status": "succeeded"},
                provider_response_id="resp-1",
                fence_owner="worker-1",
                fence_epoch=1,
                continuation_anchor=anchor_data,
            )
        )
        self.assertTrue(ok)
        backend.rpc.assert_awaited_once()
        rpc_name, rpc_payload = backend.rpc.call_args[0]
        self.assertEqual(rpc_name, "complete_continuation_request_v90")
        self.assertEqual(rpc_payload["p_continuation_anchor"], anchor_data)


class TestRequestAcceptorContinuationPlanning(unittest.TestCase):
    def setUp(self):
        self.services = MagicMock()
        self.repo = MagicMock()
        self.storage = MagicMock()
        self.settings = MagicMock()
        self.settings.execution_pool = "default"
        self.settings.default_storage_id = "default-storage"
        self.settings.material_placement_mode = "off"
        self.settings.relay_inline_cache_budget_bytes = 1048576
        self.settings.relay_cache_roll_alpha = 0.5
        self.settings.relay_grok_cache_mode = "unified_v2"
        self.settings.route_legacy_hint_mode = "warn"
        self.settings.historical_material_safety_seconds = 300
        self.settings.relay_cache_roll_max_uncached_tokens = 500
        self.settings.relay_cache_roll_min_delta_tokens = 100
        self.settings.relay_implicit_prefix_min_tokens = 50
        self.settings.relay_tail_turn_min_reserve_chars = 20
        self.settings.gemini_cache_inline_fallback_limit_bytes = 70 * 1024 * 1024
        self.settings.deployment_id = "dep-1"
        self.services.repo = self.repo
        self.services.storage = self.storage
        self.services.settings = self.settings
        self.services.headers = {}
        self.services.entry_point = "test_entry"
        self.acceptor = RequestAcceptor(self.services)

    def test_acceptor_plans_continuation_and_persists_artifact(self):
        sess_id = uuid4()
        tenant_id = "tenant-acc"
        conv_hash = "conv-acc"

        session = {
            "id": str(sess_id),
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "execution_pool": "default",
            "protocol_version": "v3.0",
            "context_policy": "conversation",
            "history_mode": "cumulative",
            "history_version": 1,
            "history_object_id": "obj-history-1",
            "instructions": "You are a helpful assistant.",
            "metadata": {
                "_relay_route": {
                    "protocol": "chat_completions",
                    "protocol_profile": {"protocol": "chat_completions"},
                    "capability_contract": {},
                }
            },
        }

        history = [
            {
                "transport_history": {
                    "user_message": {"role": "user", "content": "turn 0 query"},
                    "assistant_message": {"role": "assistant", "content": "turn 0 response"},
                }
            }
        ]
        history_fp = build_parent_head_fingerprint(history, system_instructions=session["instructions"])
        candidate_anchor = {
            "anchor_id": "anc-candidate-777",
            "history_version": 1,
            "fingerprint": history_fp,
        }

        raw_history_bytes = json.dumps(history).encode("utf-8")
        import hashlib
        history_sha = hashlib.sha256(raw_history_bytes).hexdigest()

        self.repo.get_session = AsyncMock(return_value=session)
        self.repo.get_object = AsyncMock(return_value={
            "id": "obj-history-1",
            "storage_id": "default-storage",
            "bucket": "b",
            "object_key": "k",
            "sha256": history_sha,
        })
        self.repo.find_request_by_idempotency = AsyncMock(return_value=None)
        self.repo.find_active_continuation_anchor = AsyncMock(return_value=candidate_anchor)
        self.repo.accept_request_v3 = AsyncMock(
            return_value={
                "id": str(uuid4()),
                "session_id": str(sess_id),
                "status": "queued",
                "execution_mode": "async",
            }
        )
        self.repo.create_object = AsyncMock()

        backend_storage = MagicMock()
        backend_storage.get_bytes = AsyncMock(return_value=raw_history_bytes)
        backend_storage.put_bytes = AsyncMock(
            return_value=ObjectLocation(storage_id="default-storage", bucket="b", key="k")
        )
        self.storage.get = MagicMock(return_value=backend_storage)

        artifacts_stored: dict[str, Any] = {}

        async def fake_store_json(services, *, tenant_id, conversation_hash, session_id, request_id, filename, value):
            artifacts_stored[filename] = value
            return f"obj_{filename}"

        body = SessionRequestCreate(
            input="turn 1 user input",
            execution={"mode": "async"},
            requested_cache_mode="auto",
        )

        with patch(
            "app.execution_plane.request_acceptor._store_request_json_artifact",
            side_effect=fake_store_json,
        ):
            row = asyncio.run(
                self.acceptor.accept(
                    sess_id,
                    body,
                    idempotency_key="idem-key-1",
                    owner=(tenant_id, conv_hash),
                )
            )

        self.assertIsNotNone(row)
        # 1. find_active_continuation_anchor called
        self.repo.find_active_continuation_anchor.assert_awaited_once_with(
            session_id=sess_id,
            history_version=1,
            tenant_id=tenant_id,
            conversation_hash=conv_hash,
        )

        # 2. continuation-plan.json artifact stored
        self.assertIn("continuation-plan.json", artifacts_stored)
        continuation_plan = artifacts_stored["continuation-plan.json"]
        self.assertEqual(continuation_plan["continuation_mode"], "auto")
        self.assertEqual(continuation_plan["parent_anchor_id"], "anc-candidate-777")
        self.assertEqual(continuation_plan["parent_history_version"], 1)
        self.assertEqual(continuation_plan["parent_fingerprint"], history_fp)

        # 3. request.json snapshot contains continuation_intent_plan
        self.assertIn("request.json", artifacts_stored)
        snapshot = artifacts_stored["request.json"]
        self.assertIn("continuation_intent_plan", snapshot)
        self.assertEqual(snapshot["continuation_intent_plan"]["parent_anchor_id"], "anc-candidate-777")

        # 4. create_frozen_execution_plan carries continuation_intent_plan
        frozen_plan = self.acceptor.create_frozen_execution_plan(
            row=row,
            session=session,
            snapshot=snapshot,
        )
        self.assertIsNotNone(frozen_plan.continuation_intent_plan)
        self.assertEqual(
            frozen_plan.continuation_intent_plan["parent_anchor_id"],
            "anc-candidate-777",
        )


if __name__ == "__main__":
    unittest.main()
