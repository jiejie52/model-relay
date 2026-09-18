from __future__ import annotations

import asyncio
from typing import Any

from ..storage.uploaded_files import UploadedFileStore


class MaterialResolver:
    """Authorized provider-facing view of ready canonical materials."""

    def __init__(
        self,
        repository: Any,
        store: UploadedFileStore,
        *,
        tenant_id: str,
        conversation_hash: str,
        session_id: str,
        lease_owner: str,
        binding_wait_seconds: float = 60.0,
    ) -> None:
        self.repository = repository
        self.store = store
        self.tenant_id = tenant_id
        self.conversation_hash = conversation_hash
        self.session_id = session_id
        self.lease_owner = lease_owner
        self.binding_wait_seconds = max(0.0, float(binding_wait_seconds))
        self._cache: dict[str, dict[str, Any]] = {}
        self._allowed_material_ids: set[str] | None = None

    async def _ensure_session_binding(self, material_id: str) -> None:
        if self._allowed_material_ids is None:
            self._allowed_material_ids = set(
                await self.repository.get_session_material_ids(self.session_id)
            )
        if material_id not in self._allowed_material_ids:
            raise ValueError(
                f"material is not bound to this immutable Session: {material_id}"
            )

    async def metadata(self, material_id: str) -> dict[str, Any]:
        await self._ensure_session_binding(material_id)
        if material_id in self._cache:
            return self._cache[material_id]
        row = await self.repository.get_material(
            material_id,
            tenant_id=self.tenant_id,
            conversation_hash=self.conversation_hash,
        )
        if not row or row.get("status") != "ready" or not row.get("object_key"):
            raise ValueError(f"material is not ready or not owned by this session: {material_id}")
        self._cache[material_id] = row
        return row

    async def read_bytes(self, material_id: str) -> bytes:
        row = await self.metadata(material_id)
        return await self.store.open_reader(row["object_key"])

    async def presign(self, material_id: str, *, expires_seconds: int) -> str:
        row = await self.metadata(material_id)
        return await self.store.presign_read(
            row["object_key"], expires_seconds=expires_seconds
        )
    async def claim_binding(
        self,
        *,
        material: dict[str, Any],
        provider: str,
        upstream_profile: str,
        account_scope: str,
        protocol: str,
        capability_group: str | None,
        purpose: str,
        transform_version: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.binding_wait_seconds
        while True:
            result = await self.repository.reserve_material_binding_v2(
                material_id=str(material["id"]),
                tenant_id=self.tenant_id,
                conversation_hash=self.conversation_hash,
                provider=provider,
                upstream_profile=upstream_profile,
                account_scope=account_scope,
                protocol=protocol,
                capability_group=capability_group,
                purpose=purpose,
                transform_version=transform_version,
                lease_owner=self.lease_owner,
                lease_seconds=lease_seconds,
            )
            if result.get("outcome") == "claimed":
                binding = result.get("binding") or {}
                if binding.get("id") and binding.get("lease_token"):
                    return binding
                raise ValueError("provider binding reservation returned no fencing token")
            if result.get("outcome") == "material_not_ready":
                raise ValueError(f"material is no longer ready: {material['id']}")
            if result.get("outcome") != "busy":
                raise ValueError(f"provider binding reservation failed: {result.get('outcome')}")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"provider binding is busy: {material['id']} / {purpose}")
            await asyncio.sleep(1.0)

    async def complete_binding(
        self,
        binding: dict[str, Any],
        **values: Any,
    ) -> dict[str, Any]:
        result = await self.repository.complete_material_binding_v2(
            binding_id=str(binding["id"]),
            lease_owner=self.lease_owner,
            lease_token=str(binding["lease_token"]),
            **values,
        )
        if result.get("outcome") != "ready":
            raise ValueError(f"provider binding completion lost fencing: {result.get('outcome')}")
        return result.get("binding") or {}

    async def fail_binding(self, binding: dict[str, Any], *, error_id: str | None = None) -> None:
        try:
            await self.repository.fail_material_binding_v2(
                binding_id=str(binding["id"]),
                lease_owner=self.lease_owner,
                lease_token=str(binding["lease_token"]),
                error_id=error_id,
            )
        except Exception:
            # The provider error remains primary. Binding cleanup is best-effort;
            # lease expiry still allows a later explicit execution to recover.
            return

