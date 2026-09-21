from __future__ import annotations

import json
import logging
import math
import time

import httpx
from typing import Any


_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "key",
    "secret",
    "token",
    "password",
    "x-goog-api-key",
}


def configure_logging(settings: Any) -> None:
    """Configure concise application logs and silence noisy dependency access logs."""

    level_name = str(getattr(settings, "log_level", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=False,
    )
    logging.getLogger().setLevel(level)

    # httpx emits one INFO line for every Supabase REST/RPC request.  Worker
    # claim polling and heartbeat renewal are high-frequency control-plane I/O,
    # so these library access logs are intentionally suppressed.  Relay emits
    # its own request/provider lifecycle logs below instead.
    dependency_level_name = str(
        getattr(settings, "dependency_http_log_level", "WARNING") or "WARNING"
    ).upper()
    dependency_level = getattr(logging, dependency_level_name, logging.WARNING)
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(dependency_level)

    # Uvicorn's access logger duplicates Relay's business lifecycle events and
    # makes status/result polling dominate production logs. Errors still use
    # uvicorn.error and are not suppressed.
    if not bool(getattr(settings, "uvicorn_access_log", False)):
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def now_ms() -> float:
    return time.perf_counter() * 1000.0


def elapsed_ms(start_ms: float) -> int:
    return max(0, int(round(now_ms() - start_ms)))


def status_failure_class(status_code: int | None) -> str | None:
    if status_code is None or status_code < 400:
        return None
    if 400 <= status_code < 500:
        return "client"
    return "relay"


def exception_failure_class(exc: BaseException) -> str:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if 400 <= status_code < 500:
            return "upstream_rejected"
        if status_code >= 500:
            return "upstream"
    if isinstance(exc, httpx.TimeoutException):
        return "upstream_timeout"
    if isinstance(exc, httpx.TransportError):
        return "upstream_transport"
    code = str(getattr(exc, "code", "") or "").upper()
    if "TIMEOUT" in code:
        return "upstream_timeout"
    if "PROCESSING_FAILED" in code:
        return "upstream"
    if "CONNECTION_NOT_CONFIGURED" in code:
        return "relay_configuration"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "upstream_timeout"
    if any(token in name for token in ("connect", "network", "transport", "tls", "dns", "readerror", "writeerror")):
        return "upstream_transport"
    if "supabase" in name:
        return "dependency"
    if code:
        return "relay_validation"
    return "relay"


def event(
    logger: logging.Logger,
    level: int,
    event_name: str,
    *,
    exc_info: bool | BaseException | tuple[Any, Any, Any] | None = None,
    **fields: Any,
) -> None:
    payload: dict[str, Any] = {"event": event_name}
    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = _safe_value(key, value)
    logger.log(
        level,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        exc_info=exc_info,
    )


def info(logger: logging.Logger, event_name: str, **fields: Any) -> None:
    event(logger, logging.INFO, event_name, **fields)


def warning(
    logger: logging.Logger,
    event_name: str,
    *,
    exc_info: bool | BaseException | tuple[Any, Any, Any] | None = None,
    **fields: Any,
) -> None:
    event(logger, logging.WARNING, event_name, exc_info=exc_info, **fields)


def error(
    logger: logging.Logger,
    event_name: str,
    *,
    exc_info: bool | BaseException | tuple[Any, Any, Any] | None = None,
    **fields: Any,
) -> None:
    event(logger, logging.ERROR, event_name, exc_info=exc_info, **fields)


def _safe_value(key: str, value: Any) -> Any:
    lower = key.lower()
    if lower in _SENSITIVE_KEYS or lower.endswith("_token") or lower.endswith("_secret"):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(k): _safe_value(str(k), v)
            for k, v in value.items()
            if str(k).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(key, item) for item in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
