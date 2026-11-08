from __future__ import annotations
import json
from typing import Any
from uuid import uuid4
from app.persistence.object_storage import ObjectLocation
from app.storage_paths import request_object_path_v2, session_history_request_path
from app.utils import json_bytes, utcnow

class ExecutionArtifacts:
    def __init__(self, repo, storage, settings):
        self.repo, self.storage, self.settings = repo, storage, settings

    async def load_bytes(self, object_id, *, tenant_id, conversation_hash):
        obj = await self.repo.get_object(object_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
        if not obj: raise LookupError(f"Object not found: {object_id}")
        return await self.storage.get(obj["storage_id"]).get_bytes(ObjectLocation(obj["storage_id"], obj["bucket"], obj["object_key"]))

    async def load_json(
        self, object_id: str, *, tenant_id: str, conversation_hash: str
    ) -> Any:
        obj = await self.repo.get_object(
            object_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        if not obj:
            raise LookupError(f"Object not found: {object_id}")
        data = await self.storage.get(obj["storage_id"]).get_bytes(
            ObjectLocation(obj["storage_id"], obj["bucket"], obj["object_key"])
        )
        return json.loads(data.decode("utf-8"))


    async def store_object(
        self,
        request_row: dict[str, Any],
        filename: str,
        data: bytes,
        content_type: str,
    ) -> str:
        import hashlib

        object_id = f"obj_{uuid4().hex}"
        path = request_object_path_v2(
            self.settings,
            request_row["tenant_id"],
            request_row["conversation_hash"],
            request_row["session_id"],
            request_row["id"],
            filename,
        )
        backend = self.storage.get(self.settings.default_storage_id)
        location = await backend.put_bytes(path, data, content_type=content_type)
        await self.repo.create_object(
            {
                "id": object_id,
                "tenant_id": request_row["tenant_id"],
                "conversation_hash": request_row["conversation_hash"],
                "storage_id": location.storage_id,
                "bucket": location.bucket,
                "object_key": location.key,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "content_type": content_type,
                "created_at": utcnow().isoformat(),
            }
        )
        return object_id


    async def store_history(
        self,
        request_row: dict[str, Any],
        history: list[dict[str, Any]],
        next_version: int,
    ) -> str:
        import hashlib

        data = json_bytes(history)
        object_id = f"obj_{uuid4().hex}"
        path = session_history_request_path(
            self.settings,
            request_row["tenant_id"],
            request_row["conversation_hash"],
            request_row["session_id"],
            next_version,
            request_row["id"],
        )
        backend = self.storage.get(self.settings.default_storage_id)
        location = await backend.put_bytes(path, data, content_type="application/json")
        await self.repo.create_object(
            {
                "id": object_id,
                "tenant_id": request_row["tenant_id"],
                "conversation_hash": request_row["conversation_hash"],
                "storage_id": location.storage_id,
                "bucket": location.bucket,
                "object_key": location.key,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "content_type": "application/json",
                "created_at": utcnow().isoformat(),
            }
        )
        return object_id

