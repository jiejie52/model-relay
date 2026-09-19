from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..persistence.object_storage import StorageRegistry
from ..providers.base import ProviderHTTPError
from ..storage_paths import request_object_path_v2
from ..supabase import SupabaseError
from ..utils import utcnow
from ..v2_repository import RelayV2Repository


class RawErrorRecorder:
    def __init__(
        self,
        repo: RelayV2Repository,
        storage: StorageRegistry,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.settings = settings

    async def record(
        self,
        *,
        exc: BaseException,
        source: str,
        tenant_id: str,
        conversation_hash: str,
        session_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        upstream_http_status = None
        upstream_request_id = None
        content_type = None
        content_encoding = None

        if isinstance(exc, ProviderHTTPError):
            raw = exc.body
            upstream_http_status = exc.status_code
            upstream_request_id = exc.request_id
            content_type = exc.content_type or "application/octet-stream"
            content_encoding = exc.content_encoding
        elif isinstance(exc, SupabaseError):
            raw = exc.raw_body
            upstream_http_status = exc.status_code
            content_type = exc.content_type or "application/octet-stream"
        else:
            # For non-HTTP failures there is no upstream entity body to preserve.
            # Store Relay's exact exception class/message/cause as a separate
            # Relay-generated diagnostic object and label the source correctly.
            diagnostic = {
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
                "cause_message": str(exc.__cause__) if exc.__cause__ else None,
            }
            raw = json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            content_type = "application/json; charset=utf-8"

        body_object_id = None
        body_size = len(raw)
        body_sha256 = hashlib.sha256(raw).hexdigest()
        archive_error: dict[str, Any] | None = None
        try:
            body_object_id = f"obj_{uuid4().hex}"
            path = request_object_path_v2(
                self.settings,
                tenant_id,
                conversation_hash,
                session_id,
                request_id,
                "raw-error.bin",
            )
            backend = self.storage.get(self.settings.default_storage_id)
            location = await backend.put_bytes(
                path,
                raw,
                content_type=(content_type or "application/octet-stream").split(";", 1)[0],
            )
            await self.repo.create_object(
                {
                    "id": body_object_id,
                    "tenant_id": tenant_id,
                    "conversation_hash": conversation_hash,
                    "storage_id": location.storage_id,
                    "bucket": location.bucket,
                    "object_key": location.key,
                    "sha256": body_sha256,
                    "size_bytes": body_size,
                    "content_type": content_type or "application/octet-stream",
                    "created_at": utcnow().isoformat(),
                }
            )
        except Exception as archive_exc:
            body_object_id = None
            archive_error = {
                "exception_type": type(archive_exc).__name__,
                "message": str(archive_exc),
            }

        result = {
            "source": source,
            "upstream_http_status": upstream_http_status,
            "upstream_request_id": upstream_request_id,
            "content_type": content_type,
            "content_encoding": content_encoding,
            "body_encoding": "binary",
            "body_size": body_size,
            "body_sha256": body_sha256,
            "body_object_id": body_object_id,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "archive_error": archive_error,
        }
        provider_success_object_id = getattr(exc, "provider_success_object_id", None)
        provider_output_object_id = getattr(exc, "provider_output_object_id", None)
        if provider_success_object_id:
            result["provider_success_object_id"] = provider_success_object_id
        if provider_output_object_id:
            result["provider_output_object_id"] = provider_output_object_id
        return result
