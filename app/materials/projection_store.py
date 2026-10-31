"""Session-private, content-addressed derived material artifacts.

Only deterministic local encoding is cached here, never Provider KV state.
Object metadata and bytes are persisted using the existing artifact store.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from ..core.idempotency import stable_hash
from ..observability import warning as log_warning
from ..providers.base import ProviderRequestError
from ..storage_paths import relay_object_path
from ..utils import utcnow
from .resolver import MaterialResolver

VERSION = "relay-material-inline-projection/1"


async def reusable_inline_data(*, repo: Any, fallback_store: Any, material: dict,
                               fallback: dict, binding: dict, tenant_id: str,
                               conversation_hash: str, session_id: str,
                               content_type: str, projection_version: str,
                               logger: Any) -> tuple[str, str, int, str]:
    expected = str(material.get("sha256") or "")
    identity = {
        "version": VERSION, "tenant": tenant_id, "conversation": conversation_hash,
        "session": session_id, "material": material["id"], "source": fallback.get("object_id"),
        "sha256": expected, "mime": content_type, "projection": projection_version,
        "connection": binding.get("connection_id"), "account": binding.get("account_scope_hash"),
    }
    # Missing canonical content identity can never authorize persistent reuse.
    eligible = bool(expected and session_id and fallback.get("object_id"))
    object_id = "obj_matproj_" + stable_hash(identity)
    resolver = MaterialResolver(repo, fallback_store.storage, fallback_store.settings)
    if eligible:
        try:
            obj = await repo.get_object(object_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
            if obj:
                payload = await resolver.read_object(obj)
                if hashlib.sha256(payload).hexdigest() != obj["sha256"]:
                    raise ValueError("derived artifact checksum mismatch")
                artifact = json.loads(payload)
                if (artifact.get("identity") != identity or not isinstance(artifact.get("inline_data"), str)
                        or type(artifact.get("size_bytes")) is not int):
                    raise ValueError("derived artifact identity mismatch")
                return artifact["inline_data"], expected, artifact["size_bytes"], "reused"
        except Exception as exc:
            log_warning(logger, "material_projection_read_unavailable", session_id=session_id,
                        material_id=material["id"], error_type=type(exc).__name__)

    data = await fallback_store.read(fallback)
    actual = hashlib.sha256(data).hexdigest()
    if expected and expected != actual:
        raise ProviderRequestError("MATERIAL_INTEGRITY_MISMATCH",
                                   f"Relay fallback bytes do not match canonical Material {material['id']}")
    encoded = base64.b64encode(data).decode("ascii")
    outcome = "not_persisted"
    if eligible:
        try:
            payload = json.dumps({"identity": identity, "inline_data": encoded,
                                  "size_bytes": len(data)}, separators=(",", ":"), sort_keys=True).encode()
            path = relay_object_path(fallback_store.settings, tenant_id, conversation_hash,
                                     object_id, "material-inline-projection.json")
            storage = fallback_store.storage.get(fallback_store.settings.default_storage_id)
            location = await storage.put_bytes(path, payload, content_type="application/json")
            await repo.upsert_material_projection_object({
                "id": object_id, "tenant_id": tenant_id, "conversation_hash": conversation_hash,
                "storage_id": location.storage_id, "bucket": location.bucket, "object_key": location.key,
                "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload),
                "content_type": "application/vnd.relay.material-inline-projection+json",
                "created_at": utcnow().isoformat(),
            })
            outcome = "built"
        except Exception as exc:
            log_warning(logger, "material_projection_write_unavailable", session_id=session_id,
                        material_id=material["id"], error_type=type(exc).__name__)
    return encoded, expected or actual, len(data), outcome
