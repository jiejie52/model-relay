from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..providers.base import ProviderHTTPError, ProviderTransportError
from ..storage.execution_archive import ExecutionArchiveStore
from ..storage_paths import raw_error_body_path, raw_error_headers_path
from ..supabase import SupabaseError
from ..utils import json_bytes, utcnow


class RawErrorService:
    """Lossless error archive + metadata service.

    Raw bodies remain in Supabase execution archive, never in Railway material
    storage. Metadata rows expose ownership, hashes and archive state without
    exposing internal object paths to callers.
    """

    def __init__(self, repository: Any, archive: ExecutionArchiveStore, settings: Settings) -> None:
        self.repository = repository
        self.archive = archive
        self.settings = settings

    async def capture_http(
        self,
        *,
        tenant_id: str,
        conversation_hash: str,
        origin: str,
        provider: str | None,
        service: str | None,
        status_code: int | None,
        headers: list[tuple[str, str]] | None,
        body: bytes,
        content_type: str | None,
        content_encoding: str | None,
        received_complete: bool,
        exception: dict[str, Any] | None = None,
        secondary_error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error_id = f"err_{uuid4().hex}"
        body_path = raw_error_body_path(
            self.settings, tenant_id, conversation_hash, error_id
        )
        headers_path = raw_error_headers_path(
            self.settings, tenant_id, conversation_hash, error_id
        )
        archive_state = "durable"
        body_sha = hashlib.sha256(body).hexdigest()
        try:
            await self.archive.put_bytes(
                body_path,
                body,
                content_type=content_type or "application/octet-stream",
                upsert=False,
            )
            await self.archive.put_json(
                headers_path,
                {"headers": list(headers or [])},
                upsert=False,
            )
        except Exception as archive_exc:
            # Do not let an archive failure replace the original upstream error.
            archive_state = "unavailable"
            body_path = None
            headers_path = None
            secondary_error = {
                **(secondary_error or {}),
                "archive_exception_type": type(archive_exc).__name__,
                "archive_exception_message": str(archive_exc),
            }

        row = {
            "id": error_id,
            "tenant_id": tenant_id,
            "conversation_hash": conversation_hash,
            "origin": origin,
            "provider": provider,
            "service": service,
            "http_status": status_code,
            "response_headers_object_path": headers_path,
            "body_object_path": body_path,
            "body_encoding": "binary",
            "byte_length": len(body),
            "sha256": body_sha,
            "content_type": content_type,
            "content_encoding": content_encoding,
            "received_complete": bool(received_complete),
            "archive_state": archive_state,
            "exception": exception,
            "secondary_error": secondary_error,
            "expires_at": (utcnow() + timedelta(seconds=self.settings.error_retention_seconds)).isoformat(),
        }
        try:
            created = await self.repository.create_raw_error(row)
        except Exception as meta_exc:
            # If metadata itself cannot be persisted, return an in-memory faithful
            # descriptor. The caller still keeps the original exception as primary.
            row["archive_state"] = "unavailable"
            row["secondary_error"] = {
                **(secondary_error or {}),
                "metadata_exception_type": type(meta_exc).__name__,
                "metadata_exception_message": str(meta_exc),
            }
            return row
        return created

    async def capture_provider_http(
        self,
        *,
        tenant_id: str,
        conversation_hash: str,
        exc: ProviderHTTPError,
        provider: str | None = None,
    ) -> dict[str, Any]:
        return await self.capture_http(
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            origin="provider",
            provider=provider or exc.provider,
            service=exc.service,
            status_code=exc.status_code,
            headers=exc.headers,
            body=exc.body,
            content_type=exc.content_type,
            content_encoding=exc.content_encoding,
            received_complete=exc.received_complete,
        )

    async def capture_transport(
        self,
        *,
        tenant_id: str,
        conversation_hash: str,
        exc: ProviderTransportError,
        provider: str | None = None,
    ) -> dict[str, Any]:
        return await self.capture_http(
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            origin="transport",
            provider=provider or exc.provider,
            service=exc.service,
            status_code=None,
            headers=[],
            body=b"",
            content_type=None,
            content_encoding=None,
            received_complete=False,
            exception={
                "type": exc.exception_type,
                "message": str(exc),
                "errno": exc.errno,
                "cause_chain": exc.cause_chain,
            },
        )

    async def capture_supabase(
        self,
        *,
        tenant_id: str,
        conversation_hash: str,
        exc: SupabaseError,
    ) -> dict[str, Any]:
        return await self.capture_http(
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            origin="dependency",
            provider=None,
            service="supabase",
            status_code=exc.status_code,
            headers=exc.headers,
            body=exc.body_bytes,
            content_type=None,
            content_encoding=None,
            received_complete=exc.received_complete,
        )

    async def external_view(self, row: dict[str, Any], *, include_body: bool = True) -> dict[str, Any]:
        body: bytes | None = None
        if include_body and row.get("body_object_path") and row.get("archive_state") == "durable":
            body = await self.archive.get_bytes(row["body_object_path"])

        headers: list[tuple[str, str]] | None = None
        if row.get("response_headers_object_path") and row.get("archive_state") == "durable":
            try:
                obj = await self.archive.get_json(row["response_headers_object_path"])
                if isinstance(obj, dict) and isinstance(obj.get("headers"), list):
                    headers = [tuple(x) for x in obj["headers"] if isinstance(x, (list, tuple)) and len(x) == 2]
            except Exception:
                headers = None

        result = {
            "raw_error_id": row.get("id"),
            "origin": row.get("origin"),
            "provider": row.get("provider"),
            "service": row.get("service"),
            "http_status": row.get("http_status"),
            "response_headers": headers,
            "body_encoding": "base64" if body is not None else None,
            "body": base64.b64encode(body).decode("ascii") if body is not None else None,
            "byte_length": row.get("byte_length"),
            "sha256": row.get("sha256"),
            "content_type": row.get("content_type"),
            "content_encoding": row.get("content_encoding"),
            "received_complete": row.get("received_complete"),
            "archive_state": row.get("archive_state"),
            "exception": row.get("exception"),
            "secondary_error": row.get("secondary_error"),
        }
        return result
