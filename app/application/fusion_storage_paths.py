from ..config import Settings
from ..utils import safe_segment


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
