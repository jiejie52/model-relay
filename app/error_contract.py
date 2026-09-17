from __future__ import annotations

import base64
import hashlib
from typing import Any


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_inline_body(data: bytes) -> dict[str, str]:
    try:
        return {"encoding": "utf-8", "data": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(data).decode("ascii")}


def provider_http_error_meta(
    *,
    provider: str | None,
    service: str | None,
    http_status: int,
    response_headers: list[tuple[str, str]],
    body_ref: str,
    body: bytes,
    request_id: str | None,
    received_complete: bool = True,
) -> dict[str, Any]:
    return {
        "origin": "provider",
        "provider": provider,
        "service": service,
        "http_status": http_status,
        "response_headers": [[k, v] for k, v in response_headers],
        "body_ref": body_ref,
        "byte_length": len(body),
        "sha256": sha256_hex(body),
        "received_complete": received_complete,
        "request_id": request_id,
    }


def transport_error_meta(
    *,
    provider: str | None,
    service: str | None,
    exception_type: str | None,
    message: str,
    cause_chain: list[str],
) -> dict[str, Any]:
    return {
        "origin": "transport",
        "provider": provider,
        "service": service,
        "http_status": None,
        "response_headers": [],
        "body_ref": None,
        "byte_length": None,
        "sha256": None,
        "received_complete": False,
        "exception": {
            "type": exception_type,
            "message": message,
            "cause_chain": cause_chain,
        },
    }


def relay_error_meta(code: str, message: str) -> dict[str, Any]:
    return {
        "origin": "relay",
        "provider": None,
        "service": "relay",
        "http_status": None,
        "response_headers": [],
        "body_ref": None,
        "received_complete": None,
        "relay_code": code,
        "relay_message": message,
    }


def dependency_http_error_meta(
    *,
    service: str,
    http_status: int,
    response_headers: list[tuple[str, str]],
    body: bytes,
    request_id: str | None = None,
    received_complete: bool = True,
) -> dict[str, Any]:
    """Lossless HTTP dependency error metadata.

    Unlike provider errors, dependency failures such as Supabase may make the
    object store unavailable at the same time. Keep the original body inline so
    a successful DB error commit does not silently replace/truncate the failure.
    """
    return {
        "origin": "dependency",
        "provider": None,
        "service": service,
        "http_status": http_status,
        "response_headers": [[k, v] for k, v in response_headers],
        "body": encode_inline_body(body),
        "body_ref": None,
        "byte_length": len(body),
        "sha256": sha256_hex(body),
        "received_complete": received_complete,
        "request_id": request_id,
    }
