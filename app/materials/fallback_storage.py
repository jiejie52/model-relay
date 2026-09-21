from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..persistence.object_storage import ObjectLocation, StorageRegistry
from ..storage_paths import relay_object_path
from ..utils import utcnow
from ..v2_repository import RelayV2Repository


class FallbackObjectStorage:
    """Input-file payload fallback/bridge storage.

    Relay artifacts (request snapshots, history, raw provider responses) continue
    to use the regular StorageRegistry. This wrapper is only for original input
    file bytes that policy explicitly chooses to retain.
    """

    def __init__(
        self,
        repo: RelayV2Repository,
        storage: StorageRegistry,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.settings = settings

    async def store(
        self,
        *,
        material_id: str,
        tenant_id: str,
        conversation_hash: str,
        filename: str,
        content_type: str,
        data: bytes,
        retention_policy: str,
    ) -> dict[str, Any]:
        object_id = f"obj_{uuid4().hex}"
        digest = hashlib.sha256(data).hexdigest()
        path = relay_object_path(
            self.settings,
            tenant_id,
            conversation_hash,
            object_id,
            filename,
        )
        backend = self.storage.get(self.settings.default_storage_id)
        location = await backend.put_bytes(path, data, content_type=content_type)
        await self.repo.create_object(
            {
                "id": object_id,
                "tenant_id": tenant_id,
                "conversation_hash": conversation_hash,
                "storage_id": location.storage_id,
                "bucket": location.bucket,
                "object_key": location.key,
                "sha256": digest,
                "size_bytes": len(data),
                "content_type": content_type,
                "created_at": utcnow().isoformat(),
            }
        )
        row = {
            "material_id": material_id,
            "object_id": object_id,
            "storage_id": location.storage_id,
            "bucket": location.bucket,
            "object_key": location.key,
            "sha256": digest,
            "size_bytes": len(data),
            "retention_policy": retention_policy,
            "created_at": utcnow().isoformat(),
        }
        return await self.repo.upsert_material_fallback(row)

    async def read(self, fallback: dict[str, Any]) -> bytes:
        location = ObjectLocation(
            storage_id=fallback["storage_id"],
            bucket=fallback["bucket"],
            key=fallback["object_key"],
        )
        return await self.storage.get(location.storage_id).get_bytes(location)
