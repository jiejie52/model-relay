from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..persistence.object_storage import ObjectLocation, StorageRegistry
from ..v2_repository import RelayV2Repository


@dataclass
class ResolvedMaterial:
    material: dict[str, Any]
    object: dict[str, Any]
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
        if not material or material.get("status") != "ready":
            raise LookupError(f"material is not ready: {material_id}")
        obj = await self.repo.get_object(
            material["object_id"],
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        if not obj:
            raise LookupError(f"material object is missing: {material_id}")
        data = None
        if with_bytes:
            data = await self.read_object(obj)
        return ResolvedMaterial(material, obj, data)

    async def read_object(self, obj: dict[str, Any]) -> bytes:
        location = ObjectLocation(
            storage_id=obj["storage_id"],
            bucket=obj["bucket"],
            key=obj["object_key"],
        )
        return await self.storage.get(location.storage_id).get_bytes(location)

    async def sign_object(self, obj: dict[str, Any], *, expires_in: int = 3600) -> str:
        location = ObjectLocation(
            storage_id=obj["storage_id"],
            bucket=obj["bucket"],
            key=obj["object_key"],
        )
        return await self.storage.get(location.storage_id).sign_read_url(
            location, expires_in=expires_in
        )
