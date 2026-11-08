from __future__ import annotations
from app.execution_plane.contracts import FrozenExecutionPlan
from app.persistence.execution_artifacts import ExecutionArtifacts

class ExecutionPlanLoader:
    def __init__(self, repo, storage, settings):
        self.repo = repo
        self.artifacts = ExecutionArtifacts(repo, storage, settings)

    async def load(self, row, fence):
        session = await self.repo.get_session(row["session_id"], tenant_id=row["tenant_id"], conversation_hash=row["conversation_hash"])
        if not session: raise LookupError("Relay session not found")
        snapshot = await self.artifacts.load_json(row["request_object_id"], tenant_id=row["tenant_id"], conversation_hash=row["conversation_hash"])
        history = []
        if (snapshot.get("history_mode", session.get("history_mode", "cumulative")) != "ephemeral_turn"
                and session.get("context_policy") == "conversation" and session.get("history_object_id")):
            history = await self.artifacts.load_json(session["history_object_id"], tenant_id=row["tenant_id"], conversation_hash=row["conversation_hash"])
            if not isinstance(history, list): raise ValueError("Session history is not a list")
        return FrozenExecutionPlan(row, session,
            {key: snapshot.get(key) for key in ("think_level", "effective_options", "structured_output", "requested_cache_mode")},
            snapshot.get("context_plan") or {}, snapshot.get("cache_plan") or {}, fence, snapshot, history)
