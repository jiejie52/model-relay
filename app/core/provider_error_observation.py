from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..providers.base import ProviderHTTPError

_SENSITIVE_KEYS = {
    "authorization",
    "proxy-authorization",
    "api_key",
    "apikey",
    "api-key",
    "key",
    "secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "token",
    "password",
    "credential",
    "credentials",
    "cookie",
    "set-cookie",
    "x-goog-api-key",
}

_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password)"
    r"(\s*[:=]\s*)([^\s,;\]}]+)"
)
_GOOGLE_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
_GENERIC_SECRET_RE = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}\b")


def provider_http_error_observation(
    exc: ProviderHTTPError,
    *,
    max_body_chars: int = 65536,
) -> dict[str, Any]:
    raw = bytes(exc.body or b"")
    body, body_format, truncated = _sanitize_body(raw, max_body_chars=max_body_chars)
    return {
        "status": int(exc.status_code),
        "phase": _redact_text(str(exc.phase or "")) or None,
        "request_id": pseudonymize_provider_request_id(exc.request_id),
        "body": body,
        "body_format": body_format,
        "body_size": len(raw),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "body_truncated": truncated,
    }


def safe_exception_observation(exc: BaseException, *, phase: str) -> dict[str, Any]:
    return {
        "exception_type": type(exc).__name__,
        "message": _redact_text(str(exc))[:4096],
        "phase": _redact_text(phase)[:256],
    }


def _sanitize_body(raw: bytes, *, max_body_chars: int) -> tuple[Any, str, bool]:
    if not raw:
        return "", "empty", False
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {
            "redacted_binary": True,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }, "binary_digest", False

    try:
        parsed = json.loads(text)
    except Exception:
        safe = _redact_text(text)
        truncated = len(safe) > max_body_chars
        return safe[:max_body_chars], "text", truncated

    sanitized = _sanitize_json(parsed)
    serialized = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(serialized) <= max_body_chars:
        return sanitized, "json", False
    safe = _redact_text(text)
    return safe[:max_body_chars], "json_text", True


def _sanitize_json(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _sanitize_json(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(v) for v in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower()
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
        or normalized.endswith("_credential")
    )


def _redact_text(value: str) -> str:
    value = _BEARER_RE.sub(r"\1[redacted]", value)
    value = _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", value)
    value = _GOOGLE_KEY_RE.sub("[redacted-google-api-key]", value)
    value = _GENERIC_SECRET_RE.sub("[redacted-secret]", value)
    return value


def pseudonymize_provider_request_id(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:20]}"
