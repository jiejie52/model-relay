"""Unit verification for continuation fingerprints, idempotency 2.5, and continuation intent planner."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
from app.core import idempotency
from app.core.continuation_fingerprint import (
    build_parent_head_fingerprint,
    build_turn_prefix_chain,
    extract_turn_semantics,
    verify_anchor_fingerprint,
)
from app.execution_plane.continuation_contracts import ContinuationIntentPlan
from app.execution_plane.continuation_intent_planner import ContinuationIntentPlanner
from app.runtime.continuation_contracts import ContinuationAnchorRecord


def test_continuation_fingerprint_prefix_chain():
    turn_1 = {
        "transport_history": {
            "user_content": {"role": "user", "parts": [{"text": "Hello Gemini"}]},
            "model_content": {"role": "model", "parts": [{"text": "Hello user"}]},
        },
        "material_occurrences": [
            {"material_id": "mat-1", "content_sha256": "abc123sha", "part_index": 0}
        ],
        "request_id": "req-111",
    }
    turn_2 = {
        "transport_history": {
            "kind": "chat_completions",
            "user_message": {"role": "user", "content": "What is 2+2?"},
            "assistant_message": {"role": "assistant", "content": "4"},
        },
        "request_id": "req-222",
    }
    turn_3 = {
        "transport_history": {
            "kind": "claude_messages",
            "user_message": {"role": "user", "content": "Thanks!"},
            "assistant_message": {"role": "assistant", "content": "You are welcome!"},
        }
    }
    history = [turn_1, turn_2, turn_3]

    # Prefix chain property: chain[i] == build_parent_head_fingerprint(history[:i+1])
    chain = build_turn_prefix_chain(history)
    assert len(chain) == 3
    for i in range(len(history)):
        assert chain[i] == build_parent_head_fingerprint(history[:i+1])
    assert build_parent_head_fingerprint(history) == chain[-1]

    # Anchor verification
    anchor_rec = {"anchor_id": "anc-123", "fingerprint": chain[1], "history_version": 2}
    assert verify_anchor_fingerprint(anchor_rec, chain[1]) is True
    assert verify_anchor_fingerprint(anchor_rec, chain[2]) is False


def test_idempotency_backward_compatibility_and_purity():
    assert "relay-request/2.5" in idempotency.CANONICAL_REQUEST_VERSIONS
    assert "relay-request/2.5" in idempotency.SUPPORTED_REQUEST_VERSIONS

    # Purity of caller_intent_hash
    base_intent = {"input": "test prompt", "provider": "google", "model": "gemini-2.5-flash"}
    ci_base = idempotency.caller_intent_hash(base_intent)

    # Handles must not alter hash
    ci_with_handles = dict(
        base_intent,
        previous_interaction_id="prev-123",
        upstream_handle={"handle": "xyz"},
        anchor_id="anchor-999",
    )
    assert idempotency.caller_intent_hash(ci_with_handles) == ci_base

    # Continuation mode alters hash
    assert idempotency.caller_intent_hash(dict(base_intent, continuation_mode="auto")) != ci_base

    # Historical 2.4 and 2.3 snapshots hash stably without continuation_plan_hash
    snap_24 = {
        "schema_version": "relay-request/2.4",
        "session_id": "sess-001",
        "tenant_id": "tenant-a",
        "conversation_hash": "conv-hash-1",
        "input": "hello",
        "provider": "anthropic",
        "model": "claude-3-7-sonnet",
        "effective_options": {"max_tokens": 100},
        "execution_authorization": {"auth": "granted"},
    }
    hash_24_a = idempotency.request_identity(snap_24)
    hash_24_b = idempotency.request_identity(snap_24)
    assert hash_24_a == hash_24_b

    # 2.5 snapshot hashes with continuation_plan_hash
    plan = ContinuationIntentPlan(continuation_mode="auto", parent_anchor_id="anc-1", parent_history_version=1)
    snap_25 = dict(snap_24, schema_version="relay-request/2.5", continuation_intent_plan=plan)
    hash_25 = idempotency.request_identity(snap_25)
    assert hash_25 != hash_24_a


def test_continuation_intent_planner():
    session = {"id": "sess-001", "instructions": "System prompt"}
    history = [
        {"transport_history": {"user_message": {"role": "user", "content": "hi"}, "assistant_message": {"role": "assistant", "content": "hey"}}}
    ]
    route_chat = {
        "protocol": "chat_completions",
        "protocol_profile": {"protocol": "chat_completions"},
        "capability_contract": {},
    }

    # Initial plan
    plan_initial = ContinuationIntentPlanner.plan_continuation_intent(
        session=session, body_mode="auto", candidate_anchor=None, history=history, route_profile=route_chat
    )
    assert plan_initial.continuation_mode == "auto"
    assert plan_initial.parent_anchor_id is None

    # Matching anchor
    anchor = ContinuationAnchorRecord(
        anchor_id="anc-1",
        session_id="sess-001",
        history_version=1,
        fingerprint=build_parent_head_fingerprint(history, session["instructions"]),
    )
    plan_matched = ContinuationIntentPlanner.plan_continuation_intent(
        session=session, body_mode="auto", candidate_anchor=anchor, history=history, route_profile=route_chat
    )
    assert plan_matched.continuation_mode == "auto"
    assert plan_matched.parent_anchor_id == "anc-1"
    assert plan_matched.parent_history_version == 1

    # Cache exclusivity check on gemini_native with requested_cache_mode="on"
    route_gemini = {
        "protocol": "gemini_native",
        "protocol_profile": {"protocol": "gemini_native"},
        "capability_contract": {},
    }
    plan_cache = ContinuationIntentPlanner.plan_continuation_intent(
        session=dict(session, requested_cache_mode="on"),
        body_mode="auto",
        candidate_anchor=anchor,
        history=history,
        route_profile=route_gemini,
    )
    assert plan_cache.continuation_mode == "off"


if __name__ == "__main__":
    test_continuation_fingerprint_prefix_chain()
    test_idempotency_backward_compatibility_and_purity()
    test_continuation_intent_planner()
    print("All tests passed!")