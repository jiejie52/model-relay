#!/usr/bin/env python3
"""Minimal end-to-end v2 Kimi smoke test using only Python stdlib.

Required env: RELAY_BASE_URL, RELAY_API_TOKEN, TENANT_ID, CONVERSATION_HASH.
Optional: KIMI_MODEL (default kimi-k3), POLL_SECONDS, POLL_TIMEOUT_SECONDS.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid


BASE = os.environ.get("RELAY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("RELAY_API_TOKEN", "")
TENANT = os.environ.get("TENANT_ID", "smoke-tenant")
CONV = os.environ.get("CONVERSATION_HASH", "smoke-conversation")
MODEL = os.environ.get("KIMI_MODEL", "kimi-k3")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "3"))
POLL_TIMEOUT = float(os.environ.get("POLL_TIMEOUT_SECONDS", "300"))


def request(method: str, path: str, *, payload=None, idempotency_key=None):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "X-Tenant-Id": TENANT,
        "X-Conversation-Hash": CONV,
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"raw": raw.decode("utf-8", errors="replace")}
        print(json.dumps(body, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(f"HTTP {exc.code}: {path}")


def main() -> int:
    if not TOKEN:
        print("RELAY_API_TOKEN is required", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex
    session_body = {
        "tenant_id": TENANT,
        "conversation_hash": CONV,
        "provider": "moonshot",
        "upstream_profile": "moonshot-official",
        "history_mode": "append",
        "defaults": {"model": MODEL},
        "metadata": {"smoke_run": run_id},
    }
    _, session_env = request(
        "POST",
        "/v2/sessions",
        payload=session_body,
        idempotency_key=f"smoke-session:{run_id}",
    )
    session_id = str(session_env["session"]["id"])
    print("session_id:", session_id)

    generation = (
        {"reasoning": {"effort": "high"}}
        if MODEL.lower().startswith("kimi-k3")
        else {}
    )
    job_body = {
        "input": [{"role": "user", "content": "只回答：relay smoke ok"}],
        "generation": generation,
        "metadata": {"smoke_run": run_id},
    }
    _, job_env = request(
        "POST",
        f"/v2/sessions/{session_id}/jobs",
        payload=job_body,
        idempotency_key=f"smoke-job:{run_id}",
    )
    job_id = str(job_env["job"]["id"])
    print("job_id:", job_id)

    # Recovery invariant: once job_id exists, only result/status is queried.
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        status, env = request(
            "GET",
            f"/v2/sessions/{session_id}/jobs/{job_id}/result",
        )
        state = (env.get("job") or {}).get("status")
        print("status:", state, "http:", status)
        if state == "succeeded":
            print(json.dumps(env.get("result"), ensure_ascii=False, indent=2))
            return 0
        if state in {"failed", "cancelled", "expired"}:
            print(json.dumps(env.get("error"), ensure_ascii=False, indent=2), file=sys.stderr)
            return 1
        time.sleep(POLL_SECONDS)

    print("poll timeout; keep the printed job_id and resume by status/result only", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
