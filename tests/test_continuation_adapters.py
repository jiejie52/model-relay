import unittest
from unittest.mock import AsyncMock, MagicMock
from app.config import Settings
from app.protocol_adapters.base import ProviderResult
from app.protocol_adapters.profiles.gemini_genai_sdk import (
    GeminiGenAIClient,
    extract_cache_facts,
    extract_session_facts,
    mount_continuation_handle,
)
from app.protocol_adapters.profiles.openai_compatible import OpenAICompatibleResponsesProvider
from app.protocol_adapters.profiles.responses_v2 import ResponsesV2Adapter
from app.protocol_adapters.v2_base import SealedDispatchBundle
from app.runtime.continuation_contracts import ContinuationExecutionBinding


def _make_responses_turn(user_text: str, assistant_text: str) -> dict:
    return {
        "transport_history": {
            "kind": "responses",
            "user_item": {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
            "response_output": [{"type": "output_text", "text": assistant_text}],
        }
    }


class TestResponsesV2AdapterContinuation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_legacy = MagicMock(spec=OpenAICompatibleResponsesProvider)
        self.mock_materials = MagicMock()
        self.mock_legacy.make_user_item.side_effect = lambda text: {
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }
        self.adapter = ResponsesV2Adapter(legacy=self.mock_legacy, materials=self.mock_materials)
        self.history = [
            _make_responses_turn("turn 0", "reply 0"),
            _make_responses_turn("turn 1", "reply 1"),
            _make_responses_turn("turn 2", "reply 2"),
            _make_responses_turn("turn 3", "reply 3"),
        ]

    def _make_context(self, continuation_binding=None, snapshot_extra=None):
        ctx = MagicMock(spec=SealedDispatchBundle)
        ctx.verify_integrity.return_value = True
        ctx.request_id = "req-001"
        ctx.session_id = "sess-001"
        ctx.tenant_id = "tenant-001"
        ctx.conversation_hash = "conv-hash-001"
        ctx.material_ids = []
        ctx.material_bindings = []
        ctx.history = list(self.history)
        ctx.context_plan = {}
        ctx.session = {"id": "sess-001", "history_mode": "cumulative"}
        snapshot = {
            "model": "gpt-4o",
            "input": "Current query",
            "schema_version": "relay-request/2.5",
            **(snapshot_extra or {}),
        }
        ctx.snapshot = snapshot
        ctx.continuation_binding = continuation_binding
        ctx.continuation_execution_binding = continuation_binding
        return ctx

    async def test_continuation_active_slices_history_and_injects_handle(self):
        """When continuation binding is active, legacy_history is sliced to delta turns and upstream_handle is injected."""
        binding = {
            "mechanism": "previous_interaction",
            "active_anchor_id": "anc-123",
            "upstream_handle": "resp_prev_999",
            "covered_history_version": 2,
            "fallback_occurred": False,
        }
        ctx = self._make_context(continuation_binding=binding)

        mock_provider_result = ProviderResult(
            raw_bytes=b'{"id": "resp_new_001", "model": "gpt-4o"}',
            raw_json={"id": "resp_new_001", "model": "gpt-4o"},
            text="Here is the response",
            response_id="resp_new_001",
            usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            cached_tokens=None,
            response_output=[{"type": "output_text", "text": "Here is the response"}],
            http_status=200,
            provider_request_id="req-upstream-001",
            response_headers={"content-type": "application/json"},
        )
        self.mock_legacy.execute = AsyncMock(return_value=mock_provider_result)

        result = await self.adapter.execute(ctx)

        # 1. Verify history was sliced to delta turns (turns 2 and 3)
        self.mock_legacy.execute.assert_called_once()
        call_kwargs = self.mock_legacy.execute.call_args[1]
        passed_history = call_kwargs["history"]
        self.assertEqual(len(passed_history), 2)
        self.assertEqual(passed_history[0]["user_item"]["content"][0]["text"], "turn 2")
        self.assertEqual(passed_history[1]["user_item"]["content"][0]["text"], "turn 3")

        # 2. Verify snapshot passed to legacy had continuation handle & active status
        passed_snapshot = self.mock_legacy.execute.call_args[0][0]
        self.assertEqual(passed_snapshot["previous_interaction_id"], "resp_prev_999")
        self.assertEqual(passed_snapshot["upstream_handle"], "resp_prev_999")
        self.assertEqual(passed_snapshot["active_anchor_id"], "anc-123")
        self.assertTrue(passed_snapshot["continuation_active"])

        # 3. Verify StandardizedProviderResult observed facts
        self.assertIsNotNone(result.observed)
        self.assertEqual(result.observed["upstream_handle"], "resp_new_001")
        self.assertEqual(result.observed["interaction_id"], "resp_new_001")
        self.assertEqual(result.observed["response_id"], "resp_new_001")
        self.assertTrue(result.observed["continuation_active"])
        self.assertEqual(result.observed["active_anchor_id"], "anc-123")

        # 4. Verify transport history contains new upstream_handle
        transport = result.history_entry["transport_history"]
        self.assertEqual(transport["upstream_handle"], "resp_new_001")

    async def test_continuation_all_turns_covered(self):
        """When covered_history_version == len(history), legacy_history is empty."""
        binding = ContinuationExecutionBinding(
            mechanism="delta_turn",
            active_anchor_id="anc-all",
            upstream_handle="resp_all_covered",
            fallback_occurred=False,
        )
        # Without explicit covered_history_version, covered defaults to len(history)
        ctx = self._make_context(continuation_binding=binding)

        mock_provider_result = ProviderResult(
            raw_bytes=b'{"id": "resp_new_002"}',
            raw_json={"id": "resp_new_002"},
            text="Delta response",
            response_id="resp_new_002",
            usage={},
            cached_tokens=None,
            response_output=[],
            http_status=200,
        )
        self.mock_legacy.execute = AsyncMock(return_value=mock_provider_result)

        result = await self.adapter.execute(ctx)

        call_kwargs = self.mock_legacy.execute.call_args[1]
        passed_history = call_kwargs["history"]
        self.assertEqual(len(passed_history), 0)
        self.assertEqual(result.observed["upstream_handle"], "resp_new_002")

    async def test_continuation_fallback_occurred_retains_full_history(self):
        """When fallback_occurred is True, no slicing happens and full history is sent."""
        binding = {
            "mechanism": "previous_interaction",
            "active_anchor_id": "anc-fb",
            "upstream_handle": "resp_stale",
            "covered_history_version": 2,
            "fallback_occurred": True,
            "fallback_reason": "ANCHOR_EXPIRED",
        }
        ctx = self._make_context(continuation_binding=binding)

        mock_provider_result = ProviderResult(
            raw_bytes=b'{"id": "resp_fb"}',
            raw_json={"id": "resp_fb"},
            text="Fallback response",
            response_id="resp_fb",
            usage={},
            cached_tokens=None,
            response_output=[],
            http_status=200,
        )
        self.mock_legacy.execute = AsyncMock(return_value=mock_provider_result)

        result = await self.adapter.execute(ctx)

        # Full history retained (4 turns)
        call_kwargs = self.mock_legacy.execute.call_args[1]
        passed_history = call_kwargs["history"]
        self.assertEqual(len(passed_history), 4)

        # Snapshot did NOT activate continuation
        passed_snapshot = self.mock_legacy.execute.call_args[0][0]
        self.assertNotIn("continuation_active", passed_snapshot)
        self.assertNotIn("previous_interaction_id", passed_snapshot)

        # Observed does not report continuation_active
        self.assertNotIn("continuation_active", result.observed)

    async def test_no_continuation_binding_retains_full_history(self):
        """When no continuation binding exists, standard full history is executed."""
        ctx = self._make_context(continuation_binding=None)

        mock_provider_result = ProviderResult(
            raw_bytes=b'{"id": "resp_std"}',
            raw_json={"id": "resp_std"},
            text="Standard response",
            response_id="resp_std",
            usage={},
            cached_tokens=None,
            response_output=[],
            http_status=200,
        )
        self.mock_legacy.execute = AsyncMock(return_value=mock_provider_result)

        result = await self.adapter.execute(ctx)

        call_kwargs = self.mock_legacy.execute.call_args[1]
        self.assertEqual(len(call_kwargs["history"]), 4)
        passed_snapshot = self.mock_legacy.execute.call_args[0][0]
        self.assertNotIn("continuation_active", passed_snapshot)
        self.assertEqual(result.observed["upstream_handle"], "resp_std")

    async def test_continuation_upstream_handle_dict(self):
        """When upstream_handle is a dict with previous_interaction_id or session_id, unpacked safely."""
        binding = {
            "mechanism": "previous_interaction",
            "active_anchor_id": "anc-dict",
            "upstream_handle": {"previous_interaction_id": "prev-dict-01", "session_id": "sess-upstream-01"},
            "covered_history_version": 1,
            "fallback_occurred": False,
        }
        ctx = self._make_context(continuation_binding=binding)

        mock_provider_result = ProviderResult(
            raw_bytes=b'{"interaction_id": "inter-new-99"}',
            raw_json={"interaction_id": "inter-new-99"},
            text="Dict handle response",
            response_id=None,
            usage={},
            cached_tokens=None,
            response_output=[],
            http_status=200,
        )
        self.mock_legacy.execute = AsyncMock(return_value=mock_provider_result)

        result = await self.adapter.execute(ctx)

        passed_snapshot = self.mock_legacy.execute.call_args[0][0]
        self.assertEqual(passed_snapshot["previous_interaction_id"], "prev-dict-01")
        self.assertEqual(passed_snapshot["session_id"], "sess-upstream-01")
        self.assertEqual(result.observed["upstream_handle"], "inter-new-99")
        self.assertEqual(result.observed["interaction_id"], "inter-new-99")


class TestOpenAICompatibleResponsesProviderContinuation(unittest.TestCase):
    def setUp(self):
        self.settings = MagicMock()
        self.settings.aihubmix_root = "https://aihubmix.com"
        self.provider = OpenAICompatibleResponsesProvider(self.settings)

    def test_apply_continuation_with_previous_interaction_id(self):
        payload = {}
        snapshot = {"previous_interaction_id": "prev-turn-123"}
        self.provider._apply_continuation(payload, snapshot)
        self.assertEqual(payload["previous_interaction_id"], "prev-turn-123")

    def test_apply_continuation_with_upstream_handle_str(self):
        payload = {}
        snapshot = {"upstream_handle": "handle-str-456"}
        self.provider._apply_continuation(payload, snapshot)
        self.assertEqual(payload["previous_interaction_id"], "handle-str-456")

    def test_apply_continuation_with_binding_dict(self):
        payload = {}
        snapshot = {
            "_relay_continuation_execution": {
                "mechanism": "delta_turn",
                "upstream_handle": "binding-handle-789",
                "fallback_occurred": False,
            }
        }
        self.provider._apply_continuation(payload, snapshot)
        self.assertEqual(payload["previous_interaction_id"], "binding-handle-789")

    def test_apply_continuation_fallback_skipped(self):
        payload = {}
        snapshot = {
            "_relay_continuation_execution": {
                "mechanism": "none",
                "upstream_handle": "should-not-inject",
                "fallback_occurred": True,
            }
        }
        self.provider._apply_continuation(payload, snapshot)
        self.assertNotIn("previous_interaction_id", payload)

    def test_grok_encrypted_reasoning_and_model_options_preserved(self):
        """Continuation injection does not disturb Grok reasoning or model options."""
        payload = {
            "model": "grok-beta",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "test"}]}],
            "store": False,
        }
        snapshot = {
            "model": "grok-beta",
            "provider": "grok",
            "think_level": "high",
            "previous_interaction_id": "prev-grok-001",
            "schema_version": "relay-request/2.5",
            "effective_options": {"temperature": 0.7, "max_output_tokens": 2048},
        }
        session = {"prompt_cache_key": "cache-key-grok"}

        self.provider._apply_reasoning(
            payload,
            snapshot,
            provider="grok",
            model="grok-beta",
            think_level="high",
            session=session,
        )
        self.provider._apply_continuation(payload, snapshot)
        self.provider._apply_model_options(payload, snapshot)

        # Grok encrypted content reasoning preserved
        self.assertEqual(payload["include"], ["reasoning.encrypted_content"])
        self.assertEqual(payload["prompt_cache_key"], "cache-key-grok")
        self.assertEqual(payload["reasoning"], {"effort": "high"})

        # Continuation handle safely injected
        self.assertEqual(payload["previous_interaction_id"], "prev-grok-001")

        # Model options preserved
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["max_output_tokens"], 2048)

    def test_protected_override_keys_protects_previous_interaction_id(self):
        self.assertIn("previous_interaction_id", OpenAICompatibleResponsesProvider._PROTECTED_OVERRIDE_KEYS)


class TestGeminiGenAISDKContinuation(unittest.TestCase):
    def test_mount_continuation_handle_dict(self):
        binding = {
            "mechanism": "stateful_anchor",
            "upstream_handle": "cachedContents/sample-anchor-1",
            "fallback_occurred": False,
        }
        config = {"temperature": 0.5}
        result = mount_continuation_handle(config, binding)
        self.assertEqual(result["cached_content"], "cachedContents/sample-anchor-1")
        self.assertEqual(result["cachedContent"], "cachedContents/sample-anchor-1")
        self.assertEqual(result["temperature"], 0.5)

    def test_mount_continuation_handle_none_config(self):
        binding = ContinuationExecutionBinding(
            mechanism="stateful_anchor",
            upstream_handle="cachedContents/sample-anchor-2",
            fallback_occurred=False,
        )
        result = mount_continuation_handle(None, binding)
        self.assertEqual(result, {"cached_content": "cachedContents/sample-anchor-2"})

    def test_mount_continuation_handle_fallback_skipped(self):
        binding = {
            "mechanism": "none",
            "upstream_handle": "cachedContents/stale",
            "fallback_occurred": True,
        }
        config = {"temperature": 0.5}
        result = mount_continuation_handle(config, binding)
        self.assertNotIn("cached_content", result)
        self.assertNotIn("cachedContent", result)

    def test_extract_cache_facts(self):
        response = {
            "responseId": "gemini-resp-101",
            "modelVersion": "gemini-2.5-flash",
            "usageMetadata": {
                "cachedContentTokenCount": 512,
                "promptTokenCount": 1024,
                "candidatesTokenCount": 256,
                "totalTokenCount": 1280,
            },
        }
        facts = extract_cache_facts(response)
        self.assertEqual(facts["cached_tokens"], 512)
        self.assertEqual(facts["prompt_tokens"], 1024)
        self.assertEqual(facts["completion_tokens"], 256)
        self.assertEqual(facts["total_tokens"], 1280)
        self.assertTrue(facts["has_cache"])
        self.assertEqual(facts["response_id"], "gemini-resp-101")
        self.assertEqual(facts["upstream_handle"], "gemini-resp-101")
        self.assertEqual(facts["model_version"], "gemini-2.5-flash")

    def test_extract_session_facts(self):
        response = {
            "responseId": "gemini-resp-202",
            "usageMetadata": {
                "cachedContentTokenCount": 0,
                "promptTokenCount": 100,
                "candidatesTokenCount": 50,
                "totalTokenCount": 150,
            },
        }
        binding = {
            "mechanism": "stateful_anchor",
            "active_anchor_id": "anchor-gemini-77",
            "upstream_handle": "cachedContents/gem-77",
            "fallback_occurred": False,
        }
        facts = extract_session_facts(response, continuation_binding=binding)
        self.assertFalse(facts["has_cache"])
        self.assertTrue(facts["continuation_active"])
        self.assertEqual(facts["active_anchor_id"], "anchor-gemini-77")
        self.assertEqual(facts["bound_upstream_handle"], "cachedContents/gem-77")
        self.assertEqual(facts["upstream_handle"], "gemini-resp-202")


if __name__ == "__main__":
    unittest.main()
