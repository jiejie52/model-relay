from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_hex(canonical_json_bytes(value))


def request_fingerprint(value: Any) -> str:
    return canonical_hash(value)


def scoped_job_idempotency_key(operation_scope: str, key: str) -> str:
    # Preserve the legacy (tenant_id,idempotency_key) unique index without
    # leaking the caller key into a globally reused namespace.
    digest = canonical_hash({"scope": operation_scope, "key": key})
    return f"v2:{digest}"
