from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..persistence.object_storage import ObjectLocation, StorageRegistry
from ..v2_repository import RelayV2Repository


@dataclass
class ResolvedMaterial:
    material: dict[str, Any]
    object: dict[str, Any] | None
    data: bytes | None = None


class MaterialResolver:
    def __init__(
        self,
        repo: RelayV2Repository,
        storage: StorageRegistry,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.settings = settings

    async def resolve(
        self,
        material_id: str,
        *,
        tenant_id: str,
        conversation_hash: str,
        with_bytes: bool = False,
    ) -> ResolvedMaterial:
        material = await self.repo.get_material(
            material_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        if not material or material.get("status") in {"failed", "deleted", "reupload_required"}:
            raise LookupError(f"material is not usable: {material_id}")
        obj = None
        object_id = material.get("object_id")
        if object_id:
            obj = await self.repo.get_object(
                object_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
            )
        if obj is None:
            fallback = await self.repo.get_material_fallback(material_id)
            if fallback and fallback.get("object_id"):
                obj = await self.repo.get_object(
                    fallback["object_id"],
                    tenant_id=tenant_id,
                    conversation_hash=conversation_hash,
                )
        data = None
        if with_bytes:
            if obj is None:
                raise LookupError(f"material has no Relay fallback bytes: {material_id}")
            data = await self.read_object(obj)
        return ResolvedMaterial(material, obj, data)

    async def read_object(self, obj: dict[str, Any]) -> bytes:
        location = ObjectLocation(
            storage_id=obj["storage_id"],
            bucket=obj["bucket"],
            key=obj["object_key"],
        )
        return await self.storage.get(location.storage_id).get_bytes(location)

    async def read_object_id(self, object_id: str) -> bytes:
        obj = await self.repo.get_object(object_id)
        if not obj:
            raise LookupError(f"Object not found: {object_id}")
        return await self.read_object(obj)

    async def sign_object(self, obj: dict[str, Any], *, expires_in: int = 3600) -> str:
        location = ObjectLocation(
            storage_id=obj["storage_id"],
            bucket=obj["bucket"],
            key=obj["object_key"],
        )
        return await self.storage.get(location.storage_id).sign_read_url(
            location, expires_in=expires_in
        )
