import unittest
from app.core.idempotency import stable_hash
from app.protocol_adapters.wire_projection.continuation_wire import (
    ContinuationSliceError,
    build_continuation_delta_turns,
    slice_continuation_history,
)
from app.protocol_adapters.wire_projection.gemini_physical import (
    GEMINI_PHYSICAL_LAYOUT_VERSION,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V9,
    GEMINI_PHYSICAL_LAYOUT_VERSION_V10,
    build_gemini_physical_cache_plan,
    uses_physical_layout_v10,
    frozen_session_projection_metadata_v10,
)
from app.runtime.continuation_contracts import ContinuationAnchorRecord, ContinuationExecutionBinding


def _make_gemini_turn(user_text: str, model_text: str) -> dict:
    return {
        "transport_history": {
            "kind": "gemini_native",
            "user_content": {"role": "user", "parts": [{"text": user_text}]},
            "model_content": {"role": "model", "parts": [{"text": model_text}]},
        }
    }


class TestSliceContinuationHistory(unittest.TestCase):
    def setUp(self):
        self.history = [
            {"turn": 0, "text": "hello"},
            {"turn": 1, "text": "hi"},
            {"turn": 2, "text": "how are you"},
            {"turn": 3, "text": "good"},
        ]

    def test_k_zero(self):
        """k=0 must return complete history."""
        result = slice_continuation_history(self.history, 0)
        self.assertEqual(result, self.history)
        self.assertEqual(len(result), 4)

    def test_k_len(self):
        """k=len(history) must return empty list (all covered)."""
        result = slice_continuation_history(self.history, len(self.history))
        self.assertEqual(result, [])

    def test_k_greater_than_len_fallback(self):
        """k > len(history) with default allow_fallback=True gracefully returns full history."""
        result = slice_continuation_history(self.history, 10, allow_fallback=True)
        self.assertEqual(result, self.history)

    def test_k_greater_than_len_strict(self):
        """k > len(history) with allow_fallback=False raises ContinuationSliceError."""
        with self.assertRaises(ContinuationSliceError) as ctx:
            slice_continuation_history(self.history, 10, allow_fallback=False)
        self.assertEqual(ctx.exception.code, "CONTINUATION_HISTORY_VERSION_EXCEEDED")

    def test_k_negative_fallback(self):
        """k < 0 with default allow_fallback=True gracefully returns full history."""
        result = slice_continuation_history(self.history, -1, allow_fallback=True)
        self.assertEqual(result, self.history)

    def test_k_negative_strict(self):
        """k < 0 with allow_fallback=False raises ContinuationSliceError."""
        with self.assertRaises(ContinuationSliceError) as ctx:
            slice_continuation_history(self.history, -2, allow_fallback=False)
        self.assertEqual(ctx.exception.code, "CONTINUATION_HISTORY_VERSION_NEGATIVE")

    def test_k_invalid_type_fallback(self):
        """Invalid types (e.g. str, bool) gracefully degrade with fallback=True."""
        self.assertEqual(slice_continuation_history(self.history, True, allow_fallback=True), self.history)
        self.assertEqual(slice_continuation_history(self.history, "invalid", allow_fallback=True), self.history)

    def test_k_invalid_type_strict(self):
        """Invalid types raise ContinuationSliceError with fallback=False."""
        with self.assertRaises(ContinuationSliceError) as ctx:
            slice_continuation_history(self.history, True, allow_fallback=False)
        self.assertEqual(ctx.exception.code, "CONTINUATION_HISTORY_VERSION_INVALID")

    def test_k_none(self):
        """k=None represents no valid anchor; returns full history."""
        self.assertEqual(slice_continuation_history(self.history, None), self.history)

    def test_history_none_or_empty(self):
        """None or empty history returns empty list."""
        self.assertEqual(slice_continuation_history(None, 2), [])
        self.assertEqual(slice_continuation_history([], 0), [])

    def test_normal_delta(self):
        """0 < k < len returns slice starting from index k."""
        result = slice_continuation_history(self.history, 2)
        self.assertEqual(result, [
            {"turn": 2, "text": "how are you"},
            {"turn": 3, "text": "good"},
        ])
        self.assertEqual(len(result), 2)


class TestBuildContinuationDeltaTurns(unittest.TestCase):
    def setUp(self):
        self.history = [
            {"turn": 0, "text": "t0"},
            {"turn": 1, "text": "t1"},
            {"turn": 2, "text": "t2"},
        ]

    def test_build_delta_turns_basic(self):
        res = build_continuation_delta_turns(
            self.history,
            covered_history_version=1,
            current_input="what is the weather?",
            instructions="You are a helpful assistant.",
        )
        self.assertEqual(res["schema_version"], "relay-continuation-delta-turns/1")
        self.assertEqual(res["covered_history_version"], 1)
        self.assertEqual(res["total_history_version"], 3)
        self.assertEqual(res["delta_turns_count"], 2)
        self.assertEqual(len(res["delta_history"]), 2)
        self.assertEqual(res["delta_history"], [{"turn": 1, "text": "t1"}, {"turn": 2, "text": "t2"}])
        self.assertEqual(res["bound_delta_turns"], res["delta_history"])
        self.assertEqual(res["current_input"], "what is the weather?")
        self.assertEqual(res["instructions"], "You are a helpful assistant.")
        self.assertTrue(res["is_delta"])
        self.assertTrue(bool(res["delta_hash"]))

    def test_build_delta_turns_k_zero(self):
        res = build_continuation_delta_turns(self.history, 0, "query")
        self.assertEqual(res["delta_turns_count"], 3)
        self.assertFalse(res["is_delta"])

    def test_build_delta_turns_k_all(self):
        res = build_continuation_delta_turns(self.history, 3, "query")
        self.assertEqual(res["delta_turns_count"], 0)
        self.assertEqual(res["delta_history"], [])
        self.assertTrue(res["is_delta"])


class TestGeminiPhysicalContinuation(unittest.TestCase):
    def setUp(self):
        self.history = [
            _make_gemini_turn("Hello 1", "Reply 1"),
            _make_gemini_turn("Hello 2", "Reply 2"),
            _make_gemini_turn("Hello 3", "Reply 3"),
        ]
        self.session_v8 = {
            "id": "sess-8",
            "context_policy": "conversation",
            "metadata": {"_relay_gemini_projection": {"layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION}},
        }
        self.session_v9 = {
            "id": "sess-9",
            "context_policy": "conversation",
            "metadata": {"_relay_gemini_projection": {"layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION_V9}},
        }
        self.session_v10 = {
            "id": "sess-10",
            "context_policy": "conversation",
            "metadata": {"_relay_gemini_projection": {"layout_version": GEMINI_PHYSICAL_LAYOUT_VERSION_V10}},
        }
        self.snapshot_base = {
            "model": "gemini-2.5-flash",
            "input": "Current query",
            "instructions": "Be concise.",
        }
        self.context_plan = {
            "context_plan_hash": "cplan-12345",
            "history_version": 3,
        }

    def test_stability_without_continuation_layout8(self):
        """Layout/8 without continuation retains full history in full_uncached_payload."""
        plan = build_gemini_physical_cache_plan(
            snapshot=self.snapshot_base,
            session=self.session_v8,
            history=self.history,
            material_ids=[],
            material_bindings=[],
            context_plan=self.context_plan,
        )
        self.assertEqual(plan["layout_version"], GEMINI_PHYSICAL_LAYOUT_VERSION)
        # 3 turns * 2 (user + model) + 1 current_user = 7 contents
        contents = plan["full_uncached_payload"]["contents"]
        self.assertEqual(len(contents), 7)
        self.assertEqual(contents[-1]["parts"][0]["text"], "Current query")
        self.assertNotIn("continuation_execution", plan)
        # Hash integrity check
        self.assertEqual(
            plan["physical_plan_hash"],
            stable_hash({k: v for k, v in plan.items() if k != "physical_plan_hash"}),
        )

    def test_stability_without_continuation_layout9(self):
        """Layout/9 without continuation retains full history in full_uncached_payload."""
        plan = build_gemini_physical_cache_plan(
            snapshot=self.snapshot_base,
            session=self.session_v9,
            history=self.history,
            material_ids=[],
            material_bindings=[],
            context_plan=self.context_plan,
        )
        self.assertEqual(plan["layout_version"], GEMINI_PHYSICAL_LAYOUT_VERSION_V9)
        contents = plan["full_uncached_payload"]["contents"]
        self.assertEqual(len(contents), 7)
        self.assertEqual(
            plan["physical_plan_hash"],
            stable_hash({k: v for k, v in plan.items() if k != "physical_plan_hash"}),
        )

    def test_stability_without_continuation_layout10(self):
        """Layout/10 helper and plan building works without continuation."""
        self.assertTrue(uses_physical_layout_v10(self.session_v10))
        meta_v10 = frozen_session_projection_metadata_v10()
        self.assertEqual(meta_v10["layout_version"], GEMINI_PHYSICAL_LAYOUT_VERSION_V10)

        plan = build_gemini_physical_cache_plan(
            snapshot=self.snapshot_base,
            session=self.session_v10,
            history=self.history,
            material_ids=[],
            material_bindings=[],
            context_plan=self.context_plan,
        )
        self.assertEqual(plan["layout_version"], GEMINI_PHYSICAL_LAYOUT_VERSION_V10)
        contents = plan["full_uncached_payload"]["contents"]
        self.assertEqual(len(contents), 7)
        self.assertEqual(
            plan["physical_plan_hash"],
            stable_hash({k: v for k, v in plan.items() if k != "physical_plan_hash"}),
        )

    def test_continuation_delta_stripping_layout8(self):
        """When continuation anchor covers k=2 turns, full_uncached_payload strips turns 0 and 1."""
        snapshot = {
            **self.snapshot_base,
            "_relay_continuation_execution": {
                "mechanism": "stateful_anchor",
                "active_anchor_id": "anc-001",
                "upstream_handle": "cachedContents/sample-anchor-001",
                "covered_history_version": 2,
            },
        }
        plan = build_gemini_physical_cache_plan(
            snapshot=snapshot,
            session=self.session_v8,
            history=self.history,
            material_ids=[],
            material_bindings=[],
            context_plan=self.context_plan,
        )
        # Sliced history keeps turn 2 (1 turn * 2 contents) + 1 current_user = 3 contents
        contents = plan["full_uncached_payload"]["contents"]
        self.assertEqual(len(contents), 3)
        self.assertEqual(contents[0]["parts"][0]["text"], "Hello 3")
        self.assertEqual(contents[1]["parts"][0]["text"], "Reply 3")
        self.assertEqual(contents[2]["parts"][0]["text"], "Current query")

        # continuation_handle and cachedContent mounted
        self.assertEqual(plan["full_uncached_payload"]["cachedContent"], "cachedContents/sample-anchor-001")
        self.assertEqual(plan["full_uncached_payload"]["continuation_handle"], "cachedContents/sample-anchor-001")
        # systemInstruction popped when cachedContent is mounted to avoid Google API reject
        self.assertNotIn("systemInstruction", plan["full_uncached_payload"])

        # continuation_execution metadata recorded
        self.assertIn("continuation_execution", plan)
        self.assertEqual(plan["continuation_execution"]["active_anchor_id"], "anc-001")
        self.assertEqual(plan["continuation_execution"]["covered_history_version"], 2)
        self.assertEqual(plan["continuation_execution"]["bound_delta_turns_count"], 1)

        # Hash integrity check
        self.assertEqual(plan["full_uncached_wire_hash"], stable_hash(plan["full_uncached_payload"]))
        self.assertEqual(
            plan["physical_plan_hash"],
            stable_hash({k: v for k, v in plan.items() if k != "physical_plan_hash"}),
        )

    def test_continuation_all_history_covered(self):
        """When covered_history_version == len(history), only current user content remains."""
        snapshot = {
            **self.snapshot_base,
            "_relay_continuation_execution": {
                "mechanism": "stateful_anchor",
                "active_anchor_id": "anc-all",
                "upstream_handle": "cachedContents/all-covered",
                "covered_history_version": 3,
            },
        }
        plan = build_gemini_physical_cache_plan(
            snapshot=snapshot,
            session=self.session_v9,
            history=self.history,
            material_ids=[],
            material_bindings=[],
            context_plan=self.context_plan,
        )
        contents = plan["full_uncached_payload"]["contents"]
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["parts"][0]["text"], "Current query")

    def test_continuation_covered_zero(self):
        """When covered_history_version == 0, full history is preserved."""
        snapshot = {
            **self.snapshot_base,
            "_relay_continuation_execution": {
                "mechanism": "stateful_anchor",
                "active_anchor_id": "anc-zero",
                "upstream_handle": None,
                "covered_history_version": 0,
            },
        }
        plan = build_gemini_physical_cache_plan(
            snapshot=snapshot,
            session=self.session_v9,
            history=self.history,
            material_ids=[],
            material_bindings=[],
            context_plan=self.context_plan,
        )
        contents = plan["full_uncached_payload"]["contents"]
        self.assertEqual(len(contents), 7)

    def test_continuation_fallback_occurred(self):
        """When fallback_occurred is True, continuation does not strip history."""
        snapshot = {
            **self.snapshot_base,
            "_relay_continuation_execution": {
                "mechanism": "none",
                "active_anchor_id": "anc-fb",
                "covered_history_version": 2,
                "fallback_occurred": True,
            },
        }
        plan = build_gemini_physical_cache_plan(
            snapshot=snapshot,
            session=self.session_v9,
            history=self.history,
            material_ids=[],
            material_bindings=[],
            context_plan=self.context_plan,
        )
        contents = plan["full_uncached_payload"]["contents"]
        self.assertEqual(len(contents), 7)
        self.assertNotIn("continuation_execution", plan)

    def test_continuation_via_active_anchor_record(self):
        """Continuation via ContinuationAnchorRecord in context_plan."""
        anchor = ContinuationAnchorRecord(
            anchor_id="anchor-record-01",
            session_id="sess-10",
            history_version=1,
            upstream_handle="cachedContents/record-handle",
            mechanism="stateful_anchor",
        )
        context_plan = {
            **self.context_plan,
            "active_anchor": anchor,
        }
        plan = build_gemini_physical_cache_plan(
            snapshot=self.snapshot_base,
            session=self.session_v10,
            history=self.history,
            material_ids=[],
            material_bindings=[],
            context_plan=context_plan,
        )
        # Covered version = 1: turns 1 and 2 remain (2 turns * 2 = 4) + 1 current_user = 5
        contents = plan["full_uncached_payload"]["contents"]
        self.assertEqual(len(contents), 5)
        self.assertEqual(plan["full_uncached_payload"]["cachedContent"], "cachedContents/record-handle")
        self.assertEqual(plan["continuation_execution"]["active_anchor_id"], "anchor-record-01")
        self.assertEqual(
            plan["physical_plan_hash"],
            stable_hash({k: v for k, v in plan.items() if k != "physical_plan_hash"}),
        )

    def test_continuation_opaque_handle_preserves_instruction(self):
        """Opaque handle (not cachedContents/ prefix) mounts handle and preserves systemInstruction."""
        snapshot = {
            **self.snapshot_base,
            "continuation_binding": {
                "active_anchor_id": "anchor-opaque-01",
                "upstream_handle": "session_continuation_opaque_token",
                "covered_history_version": 2,
            },
        }
        plan = build_gemini_physical_cache_plan(
            snapshot=snapshot,
            session=self.session_v9,
            history=self.history,
            material_ids=[],
            material_bindings=[],
            context_plan=self.context_plan,
        )
        self.assertEqual(
            plan["full_uncached_payload"]["continuation_handle"],
            "session_continuation_opaque_token",
        )
        self.assertNotIn("cachedContent", plan["full_uncached_payload"])
        self.assertIn("systemInstruction", plan["full_uncached_payload"])


if __name__ == "__main__":
    unittest.main()
