"""Request-local response evidence; never an execution right or retry signal."""
from __future__ import annotations

import hashlib
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.observability import warning as log_warning

logger = logging.getLogger("model-relay-upstream")
_capture: ContextVar[dict[str, Any] | None] = ContextVar("relay_response_capture", default=None)


@contextmanager
def response_capture_scope(diagnostics: dict[str, Any]):
    token = _capture.set(diagnostics)
    try:
        yield diagnostics
    finally:
        _capture.reset(token)


def current_response_capture():
    return _capture.get()


def response_diagnostics(diagnostics):
    return {k: v for k, v in diagnostics.items() if not k.startswith("_")}


def attach_response_evidence(exc: BaseException, diagnostics):
    if not diagnostics:
        return
    exc.provider_response_diagnostics = response_diagnostics(diagnostics)
    if "_raw_body" in diagnostics:
        exc.provider_response_body = diagnostics["_raw_body"]
    if diagnostics.get("body_complete") and 200 <= (diagnostics.get("http_status") or 0) < 300:
        exc.provider_success_object_id = diagnostics.get("body_object_id")


def safe_response_headers(headers):
    # Preserve diagnostic headers, but never persist credentials/cookies.
    return {str(k): ("[redacted]" if str(k).lower() in {
        "authorization", "proxy-authorization", "cookie", "set-cookie", "location"
    } or any(s in str(k).lower() for s in ("api-key", "apikey", "token", "secret", "upload-url"))
        else str(v)) for k, v in headers.items()}


async def capture_response(response, raw: bytes, *, complete: bool, diagnostics=None):
    if diagnostics is None:
        diagnostics = current_response_capture()
    if diagnostics is None:
        diagnostics = {}
    headers = dict(response.headers)
    diagnostics.update(
        phase="response_body_received" if complete else "response_body_interrupted",
        response_headers_received=True, http_status=response.status_code,
        response_headers=safe_response_headers(headers),
        content_type=headers.get("content-type"), content_encoding=headers.get("content-encoding"),
        bytes_received=len(raw), body_sha256=hashlib.sha256(raw).hexdigest(),
        body_complete=complete, body_source="provider_entity", _raw_body=raw,
        upstream_request_id=next((headers[k] for k in (
            "x-goog-request-id", "x-request-id", "request-id", "x-moonshot-request-id"
        ) if headers.get(k)), None),
    )
    if diagnostics.get("_started") is not None:
        diagnostics["elapsed_ms"] = round((time.monotonic() - diagnostics["_started"]) * 1000)
    recorder = diagnostics.get("_recorder")
    if recorder is not None:
        try:
            await recorder(raw, diagnostics)
        except Exception as exc:
            # Archiving must not replace a provider error or swallow cancellation.
            diagnostics["archive_error"] = {"exception_type": type(exc).__name__}
            log_warning(logger, "provider_response_archive_failed", request_id=diagnostics.get("request_id"),
                        session_id=diagnostics.get("session_id"), error_type=type(exc).__name__)
    return diagnostics
