from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from ..config import Settings
from ..storage_paths import relay_object_path
from ..utils import utcnow
from ..v2_repository import RelayV2Repository
from ..persistence.object_storage import StorageRegistry
from .safe_fetch import fetch_bytes


class MaterialIngress:
    def __init__(
        self,
        repo: RelayV2Repository,
        storage: StorageRegistry,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.settings = settings

    async def create(
        self,
        *,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
        filename: str,
        content_type: str | None,
        data: bytes | None = None,
        source_url: str | None = None,
        source_ref: str | None = None,
        parent_material_id: str | None = None,
        ordinal: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = await self.repo.find_material_by_idempotency(tenant_id, idempotency_key)
        if existing:
            return existing

        detected_content_type = content_type
        if source_url is not None:
            data, fetched_content_type = await fetch_bytes(
                source_url,
                max_bytes=self.settings.material_ingress_max_bytes,
                timeout_seconds=self.settings.material_ingress_timeout_seconds,
                allow_http=self.settings.material_allow_http,
            )
            detected_content_type = detected_content_type or fetched_content_type
        if data is None:
            raise ValueError("material data is required")
        if len(data) > self.settings.material_ingress_max_bytes:
            raise ValueError("material exceeds configured ingress size limit")

        detected_content_type = (detected_content_type or "application/octet-stream").split(";", 1)[0].strip()
        digest = hashlib.sha256(data).hexdigest()
        object_id = f"obj_{uuid4().hex}"
        material_id = f"mat_{uuid4().hex}"
        backend = self.storage.get(self.settings.default_storage_id)
        path = relay_object_path(
            self.settings,
            tenant_id,
            conversation_hash,
            object_id,
            filename,
        )
        location = await backend.put_bytes(path, data, content_type=detected_content_type)
        # `ready` means the durable object is already readable and byte-identical,
        # not merely that a temporary source URL was recorded.
        persisted = await backend.get_bytes(location)
        if len(persisted) != len(data) or hashlib.sha256(persisted).hexdigest() != digest:
            raise RuntimeError("persisted material integrity check failed")
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
                "content_type": detected_content_type,
                "created_at": utcnow().isoformat(),
            }
        )
        return await self.repo.create_material(
            {
                "id": material_id,
                "tenant_id": tenant_id,
                "conversation_hash": conversation_hash,
                "status": "ready",
                "idempotency_key": idempotency_key,
                "filename": filename,
                "content_type": detected_content_type,
                "size_bytes": len(data),
                "sha256": digest,
                "object_id": object_id,
                "source_ref": source_ref,
                "source_url": self._sanitized_source_url(source_url),
                "parent_material_id": parent_material_id,
                "ordinal": ordinal,
                "metadata": metadata or {},
                "created_at": utcnow().isoformat(),
            }
        )
    @staticmethod
    def _sanitized_source_url(value: str | None) -> str | None:
        if not value:
            return None
        parts = urlsplit(value)
        # Never persist signed query strings, fragments or embedded credentials.
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))

