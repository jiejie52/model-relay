from .config import Settings
from .utils import safe_segment


def _root(settings: Settings, tenant_id: str, conversation_hash: str) -> str:
    return "/".join(
        [
            settings.relay_storage_prefix.strip("/"),
            safe_segment(tenant_id),
            safe_segment(conversation_hash),
        ]
    )


def session_context_path(
    settings: Settings, tenant_id: str, conversation_hash: str, session_id: str
) -> str:
    return f"{_root(settings, tenant_id, conversation_hash)}/sessions/{session_id}/context.json"


def session_material_prefix_path(
    settings: Settings, tenant_id: str, conversation_hash: str, session_id: str
) -> str:
    # Legacy alias retained so existing sessions/checkpoints remain readable.
    return f"{_root(settings, tenant_id, conversation_hash)}/sessions/{session_id}/material-prefix.json"


def session_history_version_path(
    settings: Settings,
    tenant_id: str,
    conversation_hash: str,
    session_id: str,
    next_version: int,
    job_id: str,
) -> str:
    return (
        f"{_root(settings, tenant_id, conversation_hash)}/sessions/{session_id}/"
        f"history/history-v{next_version}-{job_id}.json"
    )


def session_turn_path(
    settings: Settings,
    tenant_id: str,
    conversation_hash: str,
    session_id: str,
    turn_id: str,
    filename: str,
) -> str:
    return f"{_root(settings, tenant_id, conversation_hash)}/sessions/{session_id}/turns/{turn_id}/{filename}"


def job_object_path(
    settings: Settings,
    tenant_id: str,
    conversation_hash: str,
    job_id: str,
    filename: str,
) -> str:
    return f"{_root(settings, tenant_id, conversation_hash)}/jobs/{job_id}/{filename}"
