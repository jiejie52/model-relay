from __future__ import annotations

from typing import Any


CALLBACK_CONTRACT_VERSION = "fusion-async-resume/v1"
CALLBACK_KIND = "dify_chatflow_resume"

_ANALYSIS_REPLAY_STAGES = {
    "material_evidence_mapping",
    "global_adjudication",
}
_FINALIZE_REPLAY_STAGES = {
    "final_evidence_review",
    "direct_final_synthesis",
    "synthesis_blueprint",
    "final_draft_generation",
    "quality_review",
    "evidence_grounded_repair",
}


def derive_resume_query(stage: str, payload: Any, job_id: str, *, continue_finalize: bool = False) -> str:
    """Return the only Dify query Relay is allowed to emit for this Fusion stage.

    The callback target itself is configured only in Railway environment variables;
    Dify-provided metadata can register conversation/user identity but cannot choose
    an arbitrary callback URL or arbitrary command.
    """
    stage = str(stage or "").strip()
    if stage == "fusion_corpus_ingest":
        return f"/fusion resume {job_id}"
    if stage in _ANALYSIS_REPLAY_STAGES:
        return "/fusion analyze"
    if stage == "scoped_decision":
        body = payload if isinstance(payload, dict) else {}
        decision_text = str(body.get("decision_text") or "").strip()
        if not decision_text:
            return ""
        query = "/fusion decide " + decision_text
        if continue_finalize:
            query += "\n/fusion finalize"
        return query
    if stage in _FINALIZE_REPLAY_STAGES:
        return "/fusion finalize"
    return ""


def callback_registration(
    *, metadata: Any, stage: str, payload: Any, job_id: str
) -> dict[str, Any]:
    meta = metadata if isinstance(metadata, dict) else {}
    raw = meta.get("async_callback")
    cfg = raw if isinstance(raw, dict) else {}
    enabled = bool(cfg.get("enabled"))
    version = str(cfg.get("contract_version") or "").strip()
    conversation_id = str(cfg.get("dify_conversation_id") or "").strip()
    user_id = str(cfg.get("dify_user_id") or "").strip()
    if not enabled or version != CALLBACK_CONTRACT_VERSION:
        return {}
    if not conversation_id or not user_id:
        return {}
    resume_query = derive_resume_query(
        stage, payload, job_id, continue_finalize=bool(cfg.get("continue_finalize"))
    )
    if not resume_query:
        return {}
    return {
        "callback_kind": CALLBACK_KIND,
        "callback_status": "waiting",
        "callback_conversation_id": conversation_id,
        "callback_user_id": user_id,
        "callback_resume_query": resume_query,
        "callback_attempt_count": 0,
        "callback_next_attempt_at": None,
        "callback_last_error": None,
        "callback_completed_at": None,
    }
