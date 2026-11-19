from __future__ import annotations

import sys
sys.path.insert(0, ".")

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import Settings
from app.control_plane.connections import ConnectionRuntime
from app.control_plane.definitions import (
    CapabilityContract,
    ConnectionSpec,
    ModelControlPlane,
    ModelOffering,
    ProtocolProfileSpec,
)
from app.control_plane.governance.workbook import compile_workbook
from app.control_plane.snapshot import SUPPORTED_PROTOCOLS
from app.core.continuation_fingerprint import continuation_head, native_prefix_chain
from app.execution_plane.continuation_intent_planner import ContinuationIntentPlanner
from app.execution_plane.result_committer import ResultCommitter
from app.protocol_adapters.factory import IMPLEMENTED_PROTOCOLS, RESOURCE_MECHANISMS
from app.protocol_adapters.profiles.gemini_interactions import GeminiInteractionsAdapter
from app.protocol_adapters.wire_projection.gemini_interactions_wire import (
    project_gemini_interactions_payload,
)
from app.runtime.continuation_binder import ContinuationBinder
from app.runtime.contracts import SealedDispatchBundle, dispatch_input_digest


def make_test_bundle(
    *,
    input_text: str = "Test prompt",
    history: list[dict[str, Any]] | None = None,
    continuation_binding: dict[str, Any] | None = None,
    instructions: str | None = "Be concise.",
    tools: list[dict[str, Any]] | None = None,
    model: str = "gemini-3.1-flash-lite",
) -> SealedDispatchBundle:
    history = history or []
    session_id = "test-session-interactions"
    request_id = "test-req-interactions-1"
    tenant_id = "tenant-test"
    conv_hash = "conv-hash-1"

    snapshot = {
        "model": model,
        "input": input_text,
        "instructions": instructions,
        "tools": tools,
        "effective_options": {"temperature": 0.5, "max_output_tokens": 2048},
        "thinking": {
            "version": "relay-thinking/2",
            "effective": {"wire_strategy": "gemini_thinking_level", "wire_action": "send_level", "level": "medium"},
        },
    }
    session = {
        "id": session_id,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "conversation_hash": conv_hash,
        "context_policy": "conversation",
    }
    digest = dispatch_input_digest(
        snapshot=snapshot,
        session=session,
        history=history,
        material_ids=[],
        material_bindings=[],
        tenant_id=tenant_id,
        conversation_hash=conv_hash,
        request_id=request_id,
        session_id=session_id,
        context_plan=None,
        cache_execution_binding=None,
        continuation_execution_binding=continuation_binding,
        execution_fence=None,
        provider_physical_plan=None,
        affinity_headers={},
    )
    return SealedDispatchBundle(
        snapshot=snapshot,
        session=session,
        history=history,
        material_ids=[],
        material_bindings=[],
        tenant_id=tenant_id,
        conversation_hash=conv_hash,
        request_id=request_id,
        session_id=session_id,
        context_plan=None,
        cache_execution_binding=None,
        continuation_execution_binding=continuation_binding,
        execution_fence=None,
        provider_physical_plan=None,
        affinity_headers={},
        seal_id=request_id,
        seal_hash=digest,
    )


class TestGeminiInteractionsWire(unittest.TestCase):
    def test_immediate_continuation_strips_covered_model_output(self):
        """When continuation hits immediate parent anchor (k == N), only delta input is sent."""
        history = [
            {
                "request_id": "req-1",
                "transport_history": {
                    "kind": "gemini_interactions",
                    "interaction_id": "interactions/anchor_turn_1",
                    "interaction_user_input": {"role": "user", "parts": [{"text": "Who directed Inception?"}]},
                    "interaction_model_steps": [
                        {"type": "thought", "signature": "sig_turn1", "text": "thinking..."},
                        {"type": "model_output", "text": "Christopher Nolan directed Inception."},
                    ],
                },
            }
        ]
        continuation_binding = {
            "mechanism": "upstream_handle",
            "active_anchor_id": "anchor-rec-1",
            "upstream_handle": "interactions/anchor_turn_1",
            "bound_delta_turns": [],  # k == 1, len(history) == 1 -> no delta turns
            "fallback_occurred": False,
            "fallback_reason": None,
        }
        bundle = make_test_bundle(
            input_text="What else did he direct?",
            history=history,
            continuation_binding=continuation_binding,
        )
        payload, user_content = project_gemini_interactions_payload(bundle)

        # Asserts
        self.assertEqual(payload["previous_interaction_id"], "interactions/anchor_turn_1")
        self.assertTrue(payload["store"])
        self.assertEqual(payload["model"], "models/gemini-3.1-flash-lite")
        # Input must contain ONLY current turn input, zero covered history or model outputs
        self.assertEqual(payload["input"], "What else did he direct?")
        payload_str = json.dumps(payload)
        self.assertNotIn("Christopher Nolan", payload_str)
        self.assertNotIn("Inception", payload_str)
        self.assertNotIn("sig_turn1", payload_str)

    def test_multiturn_delta_compensation_replays_intermediate_steps_with_signatures(self):
        """When intermediate turns exist (0 <= k < N), replays them with thought signature intact."""
        history = [
            {
                "request_id": "req-1",
                "transport_history": {
                    "kind": "gemini_interactions",
                    "interaction_id": "interactions/anchor_1",
                    "user_content": {"role": "user", "parts": [{"text": "Turn 1 question"}]},
                    "steps": [{"type": "model_output", "text": "Turn 1 answer"}],
                },
            },
            {
                "request_id": "req-2",
                "transport_history": {
                    "kind": "gemini_interactions",
                    "interaction_id": "interactions/anchor_2",
                    "interaction_user_input": {"role": "user", "parts": [{"text": "Turn 2 question"}]},
                    "interaction_model_steps": [
                        {"type": "thought", "signature": "SIG_DELTA_2", "text": "deep thought 2"},
                        {"type": "model_output", "text": "Turn 2 answer"},
                    ],
                },
            },
        ]
        # Continuation references anchor 1, so Turn 2 must be compensated in delta
        continuation_binding = {
            "mechanism": "upstream_handle",
            "active_anchor_id": "anchor-rec-1",
            "upstream_handle": "interactions/anchor_1",
            "bound_delta_turns": [history[1]],  # Turn 2 is delta
            "fallback_occurred": False,
        }
        bundle = make_test_bundle(
            input_text="Turn 3 question",
            history=history,
            continuation_binding=continuation_binding,
        )
        payload, _ = project_gemini_interactions_payload(bundle)

        self.assertEqual(payload["previous_interaction_id"], "interactions/anchor_1")
        self.assertTrue(payload["store"])
        # Input must be a list containing Turn 2 steps and Turn 3 user input
        self.assertIsInstance(payload["input"], list)
        steps = payload["input"]
        # Turn 1 output must NOT be in the payload
        payload_str = json.dumps(payload)
        self.assertNotIn("Turn 1 answer", payload_str)
        # Turn 2 question and model output and signature MUST be in payload
        self.assertIn("Turn 2 question", payload_str)
        self.assertIn("SIG_DELTA_2", payload_str)
        self.assertIn("Turn 2 answer", payload_str)
        self.assertIn("Turn 3 question", payload_str)

    def test_root_init_and_stateless_projection(self):
        """When continuation is root init or disabled, previous_interaction_id is omitted."""
        # Root init
        bundle_root = make_test_bundle(
            input_text="Root question",
            history=[],
            continuation_binding=None,
        )
        payload_root, _ = project_gemini_interactions_payload(bundle_root)
        self.assertNotIn("previous_interaction_id", payload_root)
        self.assertTrue(payload_root["store"])

        # Fallback occurred -> store=False
        bundle_fb = make_test_bundle(
            input_text="Fallback question",
            history=[],
            continuation_binding={"fallback_occurred": True, "mechanism": "none"},
        )
        payload_fb, _ = project_gemini_interactions_payload(bundle_fb)
        self.assertNotIn("previous_interaction_id", payload_fb)
        self.assertFalse(payload_fb["store"])


class TestGeminiInteractionsHistoryPrefix(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = {"id": "session-a", "context_policy": "conversation"}
        self.snapshot = {
            "protocol": "gemini_interactions",
            "session_id": self.session["id"],
            "tenant_id": "tenant-a",
            "conversation_hash": "conversation-a",
            "connection_id": "google_gemini",
            "account_scope_id": "account-a",
            "connection_spec_hash": "spec-a",
            "model": "gemini-3.1-flash-lite",
            "instructions": "Answer briefly.",
            "capability_contract": {"continuation": {
                "anchor_class": "immutable_tree_anchor",
                "verification_status": "verified",
                "requires_upstream_store": True,
                "retention_ttl_seconds": 3600,
                "max_delta_turns": 4,
                "supports_multiturn_delta": True,
            }},
        }
        self.repository = SimpleNamespace(
            list_continuation_prefixes=AsyncMock(side_effect=self.find_anchors),
        )
        self.anchors = []

    def find_anchors(self, *, head, session_id, tenant_id, conversation_hash, max_turns):
        return [
            anchor for anchor in self.anchors
            if (anchor["head_fingerprint"] == head and anchor["session_id"] == session_id
                and anchor["tenant_id"] == tenant_id and anchor["conversation_hash"] == conversation_hash
                and anchor["covered_turn_count"] <= max_turns)
        ]

    def plan(self, snapshot, history, policy="conversation"):
        return ContinuationIntentPlanner.plan_frozen_context(
            snapshot=snapshot, session=self.session, request_policy=policy, history=history,
            model_history_visible=policy == "conversation", body_mode=None, cache_mechanism=None,
        ).to_dict()

    async def bind(self, plan, snapshot, history):
        state = SimpleNamespace(
            continuation_intent_plan=plan,
            snapshot=snapshot,
            session=self.session,
            request_row={"tenant_id": snapshot["tenant_id"],
                         "conversation_hash": snapshot["conversation_hash"]},
            session_id=self.session["id"],
            history=history,
            material_bindings=[],
            historical_pinned_bindings=[],
        )
        return await ContinuationBinder(self.repository).bind_for_context(state)

    def wire(self, binding, input_text, snapshot, history=()):
        bundle = make_test_bundle(
            input_text=input_text, instructions=snapshot.get("instructions"), tools=snapshot.get("tools"),
            history=list(history), continuation_binding=binding.to_dict(),
        )
        return project_gemini_interactions_payload(bundle)

    def publish(self, snapshot, plan, binding, user_content):
        history = [{"transport_history": {
            "kind": "gemini_interactions",
            "interaction_user_input": user_content,
            "interaction_model_steps": [{"type": "model_output", "content": [{"type": "text", "text": "OK"}]}],
        }}]
        anchor = ResultCommitter._interactions_anchor(
            snapshot, plan["topology"], binding.to_dict(), history, {},
            {"id": "request-1"}, "interaction-1",
        )
        self.anchors.append({
            **anchor, "id": anchor["anchor_id"], "session_id": snapshot["session_id"],
            "tenant_id": snapshot["tenant_id"], "conversation_hash": snapshot["conversation_hash"],
        })
        return history

    async def test_long_reference_is_sent_once_with_current_round_options(self):
        first_text = "Reference material. " * 5000 + "\nQuestion 1"
        initial_plan = self.plan(self.snapshot, [])
        first = await self.bind(initial_plan, self.snapshot, [])
        first_wire, user_content = self.wire(first, first_text, self.snapshot)
        self.assertEqual(first_wire["input"], first_text)
        self.assertEqual(first_wire["system_instruction"], "Answer briefly.")
        self.assertTrue(first_wire["store"])
        self.assertNotIn("previous_interaction_id", first_wire)
        history = self.publish(self.snapshot, initial_plan, first, user_content)

        current = {**self.snapshot, "instructions": "Respond in Chinese.", "tools": [{"type": "google_search"}]}
        second_plan = self.plan(current, history)
        second = await self.bind(second_plan, current, history)
        body, _ = self.wire(second, "Question 2", current, history)

        self.assertEqual(second.effective_mode, "stateful_anchor_delta")
        self.assertEqual(body["previous_interaction_id"], "interaction-1")
        self.assertEqual(body["input"], "Question 2")
        self.assertEqual(body["system_instruction"], current["instructions"])
        self.assertEqual(body["tools"], current["tools"])

        no_settings = {**current, "instructions": None, "tools": None}
        third = await self.bind(self.plan(no_settings, history), no_settings, history)
        third_wire, _ = self.wire(third, "Another question", no_settings, history)
        self.assertEqual(third_wire["previous_interaction_id"], "interaction-1")
        self.assertNotIn("system_instruction", third_wire)
        self.assertNotIn("tools", third_wire)

        for policy in ("explicit", "session_materials_only"):
            with self.subTest(policy=policy):
                self.repository.list_continuation_prefixes.reset_mock()
                isolated = await self.bind(self.plan(current, [], policy), current, [])
                isolated_wire, _ = self.wire(isolated, "Isolated question", current)
                self.repository.list_continuation_prefixes.assert_not_awaited()
                self.assertEqual(isolated_wire["input"], "Isolated question")
                self.assertFalse(isolated_wire["store"])
                self.assertNotIn("previous_interaction_id", isolated_wire)

    async def test_legacy_frozen_plan_keeps_its_anchor_and_new_plan_replays_once(self):
        legacy_plan = self.plan(self.snapshot, [])
        legacy_topology = legacy_plan["topology"]
        legacy_topology.pop("head_format")
        legacy_topology["parent_head_fingerprint"] = continuation_head(self.snapshot)
        first = await self.bind(legacy_plan, self.snapshot, [])
        _, user_content = self.wire(first, "Reference material for the old chain.", self.snapshot)
        history = self.publish(self.snapshot, legacy_plan, first, user_content)

        legacy_topology["visible_turn_count"] = 1
        legacy_topology["turn_prefix_chain"] = native_prefix_chain(
            legacy_topology["parent_head_fingerprint"], history,
        )
        prior = await self.bind(legacy_plan, self.snapshot, history)
        self.assertEqual(prior.upstream_handle, "interaction-1")

        upgraded = await self.bind(self.plan(self.snapshot, history), self.snapshot, history)
        body, _ = self.wire(upgraded, "Continue", self.snapshot, history)
        self.assertEqual(upgraded.fallback_reason, "NO_VALID_PARENT_ANCHOR")
        self.assertNotIn("previous_interaction_id", body)
        self.assertTrue(body["store"])
        self.assertEqual(body["input"][0]["content"][0]["text"], "Reference material for the old chain.")
        self.assertEqual(body["input"][-1]["content"][0]["text"], "Continue")


class TestGeminiInteractionsAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_execute_success_and_response_parsing(self):
        """Adapter correctly parses /v1beta/interactions response and returns new anchor handle."""
        settings = Settings(
            relay_api_token="test",
            supabase_url="https://test.supabase.co",
            supabase_secret_key="test",
        )
        spec = ConnectionSpec(
            connection_id="google_gemini",
            channel_id="google_official",
            protocol="gemini_interactions",
            base_url="https://generativelanguage.googleapis.com",
            api_version="v1beta",
            auth_type="x_goog_api_key_header",
        )
        runtime = ConnectionRuntime(spec, settings, "test-api-key")

        # Mock HTTP response
        mock_response_data = {
            "id": "interactions/generated_interaction_999",
            "model": "gemini-3.1-flash-lite",
            "steps": [
                {
                    "type": "thought",
                    "signature": "thought_sig_999",
                    "text": "Internal thinking process",
                },
                {
                    "type": "model_output",
                    "text": "Final generated answer from Interactions API.",
                },
            ],
            "usage": {
                "prompt_tokens": 15,
                "candidates_tokens": 30,
                "total_tokens": 45,
                "cached_tokens": 10,
            },
        }
        mock_raw = json.dumps(mock_response_data).encode("utf-8")

        class MockStreamContext:
            def __init__(self):
                self.status_code = 200
                self.headers = {"content-type": "application/json", "x-goog-request-id": "req-xyz-123"}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def stream(self, method, url, **kwargs):
                return MockStreamContext()

        continuation_binding = {
            "mechanism": "upstream_handle",
            "active_anchor_id": "anchor-1",
            "upstream_handle": "interactions/prev_anchor_111",
            "bound_delta_turns": [],
            "fallback_occurred": False,
        }
        bundle = make_test_bundle(
            input_text="Follow-up question",
            history=[],
            continuation_binding=continuation_binding,
        )

        adapter = GeminiInteractionsAdapter(settings, connection=runtime)

        with patch("httpx.AsyncClient", MockClient), \
             patch("app.protocol_adapters.profiles.gemini_interactions.read_raw_response", AsyncMock(return_value=mock_raw)):
            result = await adapter.execute(bundle)

        # Verify standardized result
        self.assertEqual(result.response_id, "interactions/generated_interaction_999")
        self.assertEqual(result.text, "Final generated answer from Interactions API.")
        self.assertEqual(result.usage["prompt_tokens"], 15)
        self.assertEqual(result.usage["completion_tokens"], 30)
        self.assertEqual(result.cached_tokens, 10)
        self.assertEqual(result.provider_request_id, "req-xyz-123")

        # Verify observed continuation facts
        self.assertEqual(result.observed["protocol"], "gemini_interactions")
        self.assertEqual(result.observed["upstream_handle"], "interactions/generated_interaction_999")
        self.assertEqual(result.observed["continuation_handle"], "interactions/generated_interaction_999")
        self.assertEqual(result.observed["previous_interaction_id"], "interactions/prev_anchor_111")

        # Verify transport history format
        th = result.history_entry["transport_history"]
        self.assertEqual(th["kind"], "gemini_interactions")
        self.assertEqual(th["interaction_id"], "interactions/generated_interaction_999")
        self.assertEqual(len(th["interaction_model_steps"]), 2)
        self.assertEqual(th["interaction_model_steps"][0]["signature"], "thought_sig_999")


class TestControlPlaneInteractions(unittest.TestCase):
    def test_protocol_registered_in_supported_and_implemented(self):
        """gemini_interactions is in SUPPORTED_PROTOCOLS and IMPLEMENTED_PROTOCOLS."""
        self.assertIn("gemini_interactions", SUPPORTED_PROTOCOLS)
        self.assertIn("gemini_interactions", IMPLEMENTED_PROTOCOLS)
        self.assertIn("gemini_interactions", RESOURCE_MECHANISMS)
        self.assertEqual(RESOURCE_MECHANISMS["gemini_interactions"], {"implicit_prefix"})

    def test_connection_runtime_endpoint(self):
        """ConnectionRuntime generates correct /v1beta/interactions endpoint."""
        settings = Settings(
            relay_api_token="test",
            supabase_url="https://test.supabase.co",
            supabase_secret_key="test",
        )
        spec = ConnectionSpec(
            connection_id="google_gemini",
            channel_id="google_official",
            protocol="gemini_interactions",
            base_url="https://generativelanguage.googleapis.com",
            api_version="v1beta",
        )
        runtime = ConnectionRuntime(spec, settings, "key")
        self.assertEqual(
            runtime.api_url("interactions"),
            "https://generativelanguage.googleapis.com/v1beta/interactions",
        )
        self.assertEqual(runtime.auth_type, "x_goog_api_key_header")

    def test_workbook_compilation_assigns_interactions_and_immutable_tree_anchor(self):
        """Compiling workbook assigns gemini_interactions protocol and immutable_tree_anchor."""
        from pathlib import Path
        content = (Path(__file__).resolve().parents[1] / "config" / "model_management_v2.xlsx").read_bytes()
        settings = Settings(
            relay_api_token="test",
            supabase_url="https://test.supabase.co",
            supabase_secret_key="test",
            deployment_id="railway",
        )
        plane, targets = compile_workbook(content, settings)

        # Find offering for gemini-3.1-flash-lite
        matching_offering = None
        for off in plane.offerings.values():
            if off.enabled and off.model_pattern == "gemini-3.1-flash-lite":
                matching_offering = off
                break

        self.assertIsNotNone(matching_offering)
        conn = plane.connection(matching_offering.connection_id)
        prof = plane.protocol_profile(matching_offering.protocol_profile_id)
        cap = plane.contract(matching_offering.capability_contract_id)

        self.assertEqual(conn.protocol, "gemini_interactions")
        self.assertEqual(prof.protocol, "gemini_interactions")
        self.assertIsNotNone(cap.continuation)
        self.assertEqual(cap.continuation.get("anchor_class"), "immutable_tree_anchor")
        self.assertTrue(cap.continuation.get("supports_branching"))
        self.assertTrue(cap.continuation.get("supports_multiturn_delta"))
        self.assertTrue(cap.continuation.get("requires_upstream_store"))


class TestGeminiInteractionsDeploymentValidation(unittest.IsolatedAsyncioTestCase):
    async def test_verify_supply_gemini_interactions_qualifies_ready(self):
        """verify_supply qualifies gemini_interactions offering to ready state without ProbeFailure."""
        from pathlib import Path
        from unittest.mock import patch
        import httpx
        from app.control_plane.governance.deployment_validation import verify_supply
        from app.control_plane.governance.workbook import compile_workbook

        import os
        settings = Settings(
            relay_api_token="test",
            supabase_url="https://test.supabase.co",
            supabase_secret_key="test",
            deployment_id="railway",
        )
        content = (Path(__file__).resolve().parents[1] / "config" / "model_management_v2.xlsx").read_bytes()
        plane, targets = compile_workbook(content, settings)

        target_supply = None
        for s in plane.offerings.values():
            if s.model_pattern == "gemini-3.1-flash-lite" and s.connection_id == "google_gemini":
                target_supply = s
                break
        self.assertIsNotNone(target_supply)

        def mock_send(request: httpx.Request):
            url_str = str(request.url)
            if "upload" in url_str:
                if request.headers.get("X-Goog-Upload-Command") == "start":
                    return httpx.Response(
                        200,
                        headers={"x-goog-upload-url": "https://generativelanguage.googleapis.com/upload/v1beta/files?upload_id=mock123"},
                    )
                return httpx.Response(
                    200,
                    json={"file": {"name": "files/mock-test-file", "state": "ACTIVE", "uri": "https://generativelanguage.googleapis.com/v1beta/files/mock-test-file"}},
                )
            elif "files/mock-test-file" in url_str:
                if request.method == "DELETE":
                    return httpx.Response(200, json={"ok": True})
                return httpx.Response(
                    200,
                    json={"name": "files/mock-test-file", "state": "ACTIVE", "uri": "https://generativelanguage.googleapis.com/v1beta/files/mock-test-file"},
                )
            elif "interactions" in url_str:
                payload = json.loads(request.content)
                self.assertNotIn("response_format", payload)
                self.assertEqual(payload["input"][0]["type"], "document")
                self.assertEqual(payload["input"][0]["mime_type"], "application/pdf")
                level = payload.get("generation_config", {}).get("thinking_level")
                if level is not None:
                    self.assertEqual(level, level.lower())
                return httpx.Response(
                    200,
                    json={
                        "id": "interactions/probe_id", "status": "completed",
                        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "你好，这是普通文本。"}]}],
                    },
                )
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "你好，这是普通文本。"}]}}]})

        transport = httpx.MockTransport(mock_send)
        with patch.dict(os.environ, {"GOOGLE_GEMINI_API_KEY": "mock-api-key"}):
            with patch("app.control_plane.governance.deployment_validation.provider_transport", return_value=transport):
                result = await verify_supply(settings, plane, target_supply)

        self.assertEqual(result["status"], "ready")
        self.assertIn("inference", result["facets"])
        self.assertIn("provider_files_api", result["facets"])

    async def test_verify_supply_gemini_interactions_rejects_404(self):
        """An Interactions 404 must not be certified by a generateContent probe."""
        from pathlib import Path
        from unittest.mock import patch
        import httpx
        from app.control_plane.governance.deployment_validation import verify_supply
        from app.control_plane.governance.workbook import compile_workbook
        import os

        settings = Settings(
            relay_api_token="test",
            supabase_url="https://test.supabase.co",
            supabase_secret_key="test",
            deployment_id="railway",
        )
        content = (Path(__file__).resolve().parents[1] / "config" / "model_management_v2.xlsx").read_bytes()
        plane, targets = compile_workbook(content, settings)
        target_supply = next(s for s in plane.offerings.values() if s.model_pattern == "gemini-3.1-flash-lite" and s.connection_id == "google_gemini")

        def mock_send(request: httpx.Request):
            url_str = str(request.url)
            if "upload" in url_str:
                if request.headers.get("X-Goog-Upload-Command") == "start":
                    return httpx.Response(200, headers={"x-goog-upload-url": "https://generativelanguage.googleapis.com/upload/v1beta/files?upload_id=mock123"})
                return httpx.Response(200, json={"file": {"name": "files/mock-test-file", "state": "ACTIVE", "uri": "https://generativelanguage.googleapis.com/v1beta/files/mock-test-file"}})
            elif "files/mock-test-file" in url_str:
                if request.method == "DELETE":
                    return httpx.Response(200, json={"ok": True})
                return httpx.Response(200, json={"name": "files/mock-test-file", "state": "ACTIVE", "uri": "https://generativelanguage.googleapis.com/v1beta/files/mock-test-file"})
            elif "interactions" in url_str:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            elif "generateContent" in url_str:
                self.fail("Interactions validation must never dispatch generateContent")
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(mock_send)
        with patch.dict(os.environ, {"GOOGLE_GEMINI_API_KEY": "mock-api-key"}):
            with patch("app.control_plane.governance.deployment_validation.provider_transport", return_value=transport):
                result = await verify_supply(settings, plane, target_supply)

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["reason"], "upstream_http_404")


if __name__ == "__main__":
    unittest.main()
