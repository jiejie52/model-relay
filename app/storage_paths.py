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


def session_material_prefix_path(
    settings: Settings, tenant_id: str, conversation_hash: str, session_id: str
) -> str:
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


def _fusion_root(settings: Settings, tenant_id: str, conversation_hash: str, corpus_id: str) -> str:
    return "/".join(
        [
            "fusion",
            safe_segment(tenant_id),
            safe_segment(conversation_hash),
            safe_segment(corpus_id),
        ]
    )


def fusion_corpus_manifest_path(
    settings: Settings, tenant_id: str, conversation_hash: str, corpus_id: str
) -> str:
    return f"{_fusion_root(settings, tenant_id, conversation_hash, corpus_id)}/corpus/manifest.json"


def fusion_material_object_path(
    settings: Settings,
    tenant_id: str,
    conversation_hash: str,
    corpus_id: str,
    material_id: str,
    filename: str,
) -> str:
    return (
        f"{_fusion_root(settings, tenant_id, conversation_hash, corpus_id)}/"
        f"materials/{safe_segment(material_id)}/{safe_segment(filename)}"
    )


def fusion_artifact_object_path(
    settings: Settings,
    tenant_id: str,
    conversation_hash: str,
    corpus_id: str,
    artifact_id: str,
    filename: str,
) -> str:
    return (
        f"{_fusion_root(settings, tenant_id, conversation_hash, corpus_id)}/"
        f"artifacts/{safe_segment(artifact_id)}/{safe_segment(filename)}"
    )


def relay_object_path(
    settings: Settings,
    tenant_id: str,
    conversation_hash: str,
    object_id: str,
    filename: str,
) -> str:
    return (
        f"{_root(settings, tenant_id, conversation_hash)}/objects/"
        f"{safe_segment(object_id)}/{safe_segment(filename)}"
    )


def request_object_path_v2(
    settings: Settings,
    tenant_id: str,
    conversation_hash: str,
    session_id: str,
    request_id: str,
    filename: str,
) -> str:
    return (
        f"{_root(settings, tenant_id, conversation_hash)}/sessions/"
        f"{safe_segment(session_id)}/requests/{safe_segment(request_id)}/"
        f"{safe_segment(filename)}"
    )


def session_history_request_path(
    settings: Settings,
    tenant_id: str,
    conversation_hash: str,
    session_id: str,
    next_version: int,
    request_id: str,
) -> str:
    return (
        f"{_root(settings, tenant_id, conversation_hash)}/sessions/"
        f"{safe_segment(session_id)}/history/"
        f"history-v{next_version}-{safe_segment(request_id)}.json"
    )
