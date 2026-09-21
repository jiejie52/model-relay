from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from ..providers.base import ProviderRequestError
from ..observability import error as log_error
from ..utils import utcnow
from ..v2_repository import RelayV2Repository
from .fallback_storage import FallbackObjectStorage
from .gemini_transport import (
    GEMINI_EXTERNAL_URL_REPRESENTATION,
    external_url_binding,
)
from .provider_files.base import MaterialFile
from .provider_files.registry import ProviderFileRegistry


logger = logging.getLogger("model-relay-material-bindings")


class BindingResolver:
    def __init__(
        self,
        repo: RelayV2Repository,
        fallback: FallbackObjectStorage,
        file_adapters: ProviderFileRegistry,
    ) -> None:
        self.repo = repo
        self.fallback = fallback
        self.file_adapters = file_adapters

    async def freeze_for_request(
        self,
        *,
        material_ids: list[str],
        connection_id: str,
        tenant_id: str,
        conversation_hash: str,
        existing_snapshot: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        # Once a Request has frozen bindings, retries reuse those exact provider
        # identities instead of silently switching generations. The frozen
        # snapshot must still belong to the Session's private route.
        adapter = self.file_adapters.maybe_get(connection_id)
        if existing_snapshot:
            for item in existing_snapshot:
                frozen_connection = str(item.get("connection_id") or "")
                if frozen_connection != connection_id:
                    log_error(
                        logger,
                        "route_binding_mismatch",
                        material_id=item.get("material_id"),
                        expected_connection_id=connection_id,
                        frozen_connection_id=frozen_connection,
                        failure_class="relay_validation",
                    )
                    raise ProviderRequestError(
                        "ROUTE_BINDING_MISMATCH",
                        "Frozen material binding does not match the Session route",
                    )
                if adapter is not None:
                    frozen_scope = str(item.get("account_scope_hash") or "")
                    if frozen_scope != adapter.account_scope_hash:
                        log_error(
                            logger,
                            "route_binding_mismatch",
                            material_id=item.get("material_id"),
                            connection_id=connection_id,
                            expected_account_scope_hash=adapter.account_scope_hash,
                            frozen_account_scope_hash=frozen_scope,
                            failure_class="relay_validation",
                        )
                        raise ProviderRequestError(
                            "ROUTE_BINDING_MISMATCH",
                            "Frozen material binding belongs to a different Provider account scope",
                        )
            return [dict(item) for item in existing_snapshot]

        snapshots: list[dict[str, Any]] = []
        for material_id in material_ids:
            material = await self.repo.get_material(
                material_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
            )
            if not material:
                raise ProviderRequestError("MATERIAL_NOT_FOUND", f"Material not found: {material_id}")
            if material.get("status") in {"failed", "deleted", "reupload_required"}:
                raise ProviderRequestError(
                    "MATERIAL_REUPLOAD_REQUIRED",
                    f"Material is not usable and must be uploaded again: {material_id}",
                )

            if adapter is None:
                fallback = await self.repo.get_material_fallback(material_id)
                if not fallback:
                    raise ProviderRequestError(
                        "MATERIAL_REUPLOAD_REQUIRED",
                        f"Connection {connection_id} requires Relay fallback bytes but material {material_id} has none",
                    )
                snapshots.append(
                    {
                        "material_id": material_id,
                        "binding_generation": int(material.get("binding_generation") or 0),
                        "binding_kind": "fallback_object",
                        "connection_id": connection_id,
                        "account_scope_hash": "relay-fallback",
                        "content_sha256": material.get("sha256"),
                        "content_type": material.get("content_type"),
                        "filename": material.get("filename"),
                        "object_id": fallback.get("object_id"),
                    }
                )
                continue

            binding = await self.repo.get_provider_binding(
                material_id=material_id,
                connection_id=connection_id,
                account_scope_hash=adapter.account_scope_hash,
            )
            if not self._binding_usable(binding):
                fallback = await self.repo.get_material_fallback(material_id)
                if not fallback:
                    await self.repo.update_material(
                        material_id,
                        {"status": "reupload_required", "durability": "reupload_required"},
                    )
                    raise ProviderRequestError(
                        "MATERIAL_REUPLOAD_REQUIRED",
                        f"Provider binding is unavailable and no fallback bytes exist: {material_id}",
                    )

                generation = int((binding or {}).get("generation") or material.get("binding_generation") or 0) + 1
                if self._uses_gemini_external_url(material, binding, adapter):
                    ttl_seconds = max(300, min(
                        int(self.fallback.settings.supabase_signed_url_ttl),
                        604800,
                    ))
                    signed_url = await self.fallback.sign_read_url(
                        fallback, expires_in=ttl_seconds
                    )
                    binding = external_url_binding(
                        material_id=material_id,
                        connection_id=connection_id,
                        account_scope_hash=adapter.account_scope_hash,
                        external_url=signed_url,
                        object_id=str(fallback["object_id"]),
                        generation=generation,
                        ttl_seconds=ttl_seconds,
                        metadata={
                            "transport": "supabase_external_url",
                            "storage_id": fallback.get("storage_id"),
                            "object_id": fallback.get("object_id"),
                            "refresh": True,
                        },
                    )
                else:
                    data = await self.fallback.read(fallback)
                    result = await adapter.prepare(
                        MaterialFile(
                            material_id=material_id,
                            tenant_id=tenant_id,
                            conversation_hash=conversation_hash,
                            filename=str(material.get("filename") or material_id),
                            content_type=str(material.get("content_type") or "application/octet-stream"),
                            size_bytes=len(data),
                            sha256=str(material.get("sha256") or ""),
                            data=data,
                        ),
                        generation=generation,
                    )
                    binding = dict(result.binding)
                    binding["material_id"] = material_id

                binding.setdefault("created_at", utcnow().isoformat())
                binding["updated_at"] = utcnow().isoformat()
                await self.repo.upsert_provider_binding(binding)
                await self.repo.update_material(
                    material_id,
                    {
                        "status": "ready_provider",
                        "binding_generation": generation,
                        "durability": "relay_backed",
                    },
                )

            snapshots.append(self._snapshot(material, binding))
        return snapshots

    @staticmethod
    def _uses_gemini_external_url(
        material: dict[str, Any],
        binding: dict[str, Any] | None,
        adapter: Any,
    ) -> bool:
        if str(getattr(adapter, "provider", "") or "").lower() != "gemini":
            return False
        if binding and str(binding.get("representation") or "") == GEMINI_EXTERNAL_URL_REPRESENTATION:
            return True
        metadata = material.get("metadata") if isinstance(material.get("metadata"), dict) else {}
        policy = metadata.get("_relay_gemini_transport") if isinstance(metadata.get("_relay_gemini_transport"), dict) else {}
        return str(policy.get("mode") or "") == "supabase_external_url"

    @staticmethod
    def _binding_usable(binding: dict[str, Any] | None) -> bool:
        if not binding:
            return False
        state = str(binding.get("state") or binding.get("processing_state") or "").lower()
        if state not in {"active", "ready", "processed"}:
            return False
        expires = binding.get("expires_at")
        if not expires:
            return True
        try:
            dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt > utcnow()
        except Exception:
            return False

    @staticmethod
    def _snapshot(material: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
        return {
            "material_id": material["id"],
            "binding_generation": int(binding.get("generation") or 1),
            "binding_kind": binding.get("representation") or binding.get("purpose") or "provider_file",
            "connection_id": binding.get("connection_id"),
            "account_scope_hash": binding.get("account_scope_hash"),
            "provider": binding.get("provider"),
            "purpose": binding.get("purpose"),
            "representation": binding.get("representation"),
            "external_file_id": binding.get("external_file_id") or binding.get("provider_file_id"),
            "external_uri": binding.get("external_uri") or binding.get("file_uri"),
            "content_sha256": material.get("sha256"),
            "content_type": material.get("content_type"),
            "filename": material.get("filename"),
            "metadata": binding.get("metadata") or {},
        }
