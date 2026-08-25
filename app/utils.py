import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any


_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def utcnow() -> datetime:
    return datetime.now(UTC)


def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def safe_segment(value: str, fallback: str = "unknown") -> str:
    cleaned = _SAFE_SEGMENT.sub("_", value.strip()).strip("._-")
    return (cleaned or fallback)[:180]


def stable_prompt_cache_key(conversation_hash: str) -> str:
    digest = hashlib.sha256(conversation_hash.encode("utf-8")).hexdigest()
    return f"dify-relay-{digest[:40]}"


def truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    clipped = raw[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def compact_error_excerpt(raw: bytes | str, max_bytes: int = 4096) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    return truncate_utf8(text, max_bytes)
