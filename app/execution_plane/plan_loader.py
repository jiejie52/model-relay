from __future__ import annotations
from app.execution_plane.contracts import FrozenExecutionPlan
from app.persistence.execution_artifacts import ExecutionArtifacts
from app.core.idempotency import stable_hash, request_identity
from app.shared_contracts import ExecutionConflictError

class ExecutionPlanLoader:
    def __init__(self, repo, storage, settings):
        self.repo = repo
        self.artifacts = ExecutionArtifacts(repo, storage, settings)

    async def load(self, row, fence):
        session = await self.repo.get_session(row["session_id"], tenant_id=row["tenant_id"], conversation_hash=row["conversation_hash"])
        if not session: raise LookupError("Relay session not found")
        snapshot = await self.artifacts.load_json(row["request_object_id"], tenant_id=row["tenant_id"], conversation_hash=row["conversation_hash"])
        if not isinstance(snapshot, dict):
            raise ExecutionConflictError("Stored Request snapshot is not an object")
        frozen_route = (session.get("metadata") or {}).get("_relay_route") or {}
        for field in ("connection_identity_hash", "upstream_model",'connection_contract_version','connection_spec_hash','account_scope_id'):
            if frozen_route.get(field) and snapshot.get(field) != frozen_route[field]:
                raise ExecutionConflictError("Stored Request differs from its frozen connection contract")
        if row.get("request_hash") and request_identity(snapshot) != row["request_hash"]:
            raise ExecutionConflictError("Stored Request identity changed")
        for key, digest in (("context_plan", "context_plan_hash"), ("cache_plan", "plan_hash")):
            value = snapshot.get(key)
            if isinstance(value, dict) and value.get(digest) and stable_hash({k: v for k, v in value.items() if k != digest}) != value[digest]:
                raise ExecutionConflictError("Stored logical plan checksum mismatch")
        if int(session.get("history_version") or 0) != int(row.get("expected_history_version") or 0):
            raise ExecutionConflictError("Session history no longer matches the accepted Request")
        history = []
        if (snapshot.get("history_mode", session.get("history_mode", "cumulative")) != "ephemeral_turn"
                and session.get("context_policy") == "conversation" and session.get("history_object_id")):
            history = await self.artifacts.load_json(session["history_object_id"], tenant_id=row["tenant_id"], conversation_hash=row["conversation_hash"])
            if not isinstance(history, list): raise ValueError("Session history is not a list")
        return FrozenExecutionPlan(row, session,
            {key: snapshot.get(key) for key in ("think_level", "thinking", "effective_options", "structured_output", "requested_cache_mode")},
            snapshot.get("context_plan") or {}, snapshot.get("cache_plan") or {}, fence, snapshot, history)
