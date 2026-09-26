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

    async def sign_read_url(
        self,
        fallback: dict[str, Any],
        *,
        expires_in: int | None = None,
    ) -> str:
        location = ObjectLocation(
            storage_id=fallback["storage_id"],
            bucket=fallback["bucket"],
            key=fallback["object_key"],
        )
        ttl = int(expires_in or self.settings.supabase_signed_url_ttl)
        ttl = max(300, min(ttl, 604800))
        return await self.storage.get(location.storage_id).sign_read_url(
            location, expires_in=ttl
        )

    async def delete_object_only(self, fallback: dict[str, Any]) -> None:
        """Delete one stored object without touching the material fallback row.

        This is used when a Gemini External URL bridge object is replaced by a
        provider-projected copy.  The material fallback row already points at the
        replacement, so deleting the old row would remove the new mapping.
        """
        object_id = str(fallback["object_id"])
        location = ObjectLocation(
            storage_id=fallback["storage_id"],
            bucket=fallback["bucket"],
            key=fallback["object_key"],
        )
        await self.storage.get(location.storage_id).delete(location)
        await self.repo.delete_object(object_id)

    async def delete(self, fallback: dict[str, Any]) -> None:
        """Delete an input-file fallback object and its metadata.

        Used by Gemini request-level promotion when the authoritative sum of
        request materials crosses the Files API threshold. Request/history/raw
        artifacts are untouched; this only removes the original input payload.
        """
        material_id = str(fallback["material_id"])
        object_id = str(fallback["object_id"])
        location = ObjectLocation(
            storage_id=fallback["storage_id"],
            bucket=fallback["bucket"],
            key=fallback["object_key"],
        )
        await self.storage.get(location.storage_id).delete(location)
        await self.repo.update_material(material_id, {"object_id": None})
        await self.repo.delete_material_fallback(material_id)
        await self.repo.delete_object(object_id)
