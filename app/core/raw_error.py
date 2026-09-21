from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..persistence.object_storage import StorageRegistry
from ..providers.base import ProviderHTTPError
from ..providers.http_wire import decode_entity
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
        upstream_headers: dict[str, str] | None = None
        phase: str | None = None

        if isinstance(exc, ProviderHTTPError):
            raw = exc.body
            upstream_http_status = exc.status_code
            upstream_request_id = exc.request_id
            content_type = exc.content_type or "application/octet-stream"
            content_encoding = exc.content_encoding
            upstream_headers = dict(exc.response_headers) if getattr(exc, "response_headers", None) else None
            phase = getattr(exc, "phase", None)
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

        inline_body = raw_body_inline_fields(
            raw,
            content_type=content_type,
            content_encoding=content_encoding,
        )
        body_text = inline_body["body_text"]
        body_encoding = inline_body["body_encoding"]
        # body_base64 is the exact upstream entity bytes. body_text is an
        # additional lossless text view when the declared/textual encoding can
        # be decoded strictly. Neither representation is truncated.
        result = {
            "source": source,
            "upstream_http_status": upstream_http_status,
            "upstream_request_id": upstream_request_id,
            "upstream_headers": upstream_headers,
            "phase": phase,
            "content_type": content_type,
            "content_encoding": content_encoding,
            "body_encoding": body_encoding,
            "body_size": body_size,
            "body_sha256": body_sha256,
            "body_object_id": body_object_id,
            "body_text": body_text,
            "body_base64": inline_body["body_base64"],
            "exception_type": type(exc).__name__,
            # HTTP error bodies are authoritative. Expose the complete textual
            # body as message as well so callers that only surface `message`
            # still receive the provider's original response instead of Relay's
            # generic ProviderHTTPError label.
            "message": (
                body_text
                if body_text is not None and isinstance(exc, (ProviderHTTPError, SupabaseError))
                else str(exc)
            ),
            "archive_error": archive_error,
        }
        provider_success_object_id = getattr(exc, "provider_success_object_id", None)
        provider_output_object_id = getattr(exc, "provider_output_object_id", None)
        if provider_success_object_id:
            result["provider_success_object_id"] = provider_success_object_id
        if provider_output_object_id:
            result["provider_output_object_id"] = provider_output_object_id
        return result


def raw_body_inline_fields(
    raw: bytes,
    *,
    content_type: str | None,
    content_encoding: str | None,
) -> dict[str, Any]:
    body_text, body_encoding = _decode_body_text(
        raw,
        content_type=content_type,
        content_encoding=content_encoding,
    )
    return {
        "body_encoding": body_encoding,
        "body_text": body_text,
        "body_base64": base64.b64encode(raw).decode("ascii"),
    }


def _decode_body_text(
    raw: bytes,
    *,
    content_type: str | None,
    content_encoding: str | None,
) -> tuple[str | None, str]:
    """Return a strict, non-lossy text view plus the character encoding.

    The SHA/size/base64 fields always describe the exact raw upstream entity
    bytes. For textual media types we additionally decode Content-Encoding and
    then the declared/default character set. If strict decoding fails, callers
    still receive the exact bytes through body_base64 and body_encoding=binary.
    """
    media_type, charset = _parse_content_type(content_type)
    textual = (
        media_type.startswith("text/")
        or media_type == "application/json"
        or media_type.endswith("+json")
        or media_type in {"application/xml", "application/javascript", "application/x-www-form-urlencoded"}
        or media_type.endswith("+xml")
    )
    if not textual:
        return None, "binary"

    try:
        entity = decode_entity(raw, content_encoding)
    except Exception:
        return None, "binary"

    encoding = charset or "utf-8"
    try:
        return entity.decode(encoding, errors="strict"), encoding.lower()
    except (LookupError, UnicodeDecodeError):
        return None, "binary"


def _parse_content_type(content_type: str | None) -> tuple[str, str | None]:
    if not content_type:
        return "", None
    parts = [part.strip() for part in str(content_type).split(";")]
    media_type = parts[0].lower()
    charset = None
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip().lower() == "charset":
            charset = value.strip().strip('"').strip("'") or None
            break
    return media_type, charset
