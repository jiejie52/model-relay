from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["dify-relay-gateway"])

def _json_or_text(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"detail": resp.text}

def _forward_headers(
    authorization: Optional[str],
    idempotency_key: Optional[str],
    tenant_id: Optional[str],
    conversation_hash: Optional[str],
) -> Dict[str, str]:
    h: Dict[str, str] = {}
    if authorization:
        h["Authorization"] = authorization
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key
    if tenant_id:
        h["X-Tenant-Id"] = tenant_id
    if conversation_hash:
        h["X-Conversation-Hash"] = conversation_hash
    return h

async def _internal_client(request: Request) -> httpx.AsyncClient:
    timeout = float(os.getenv("DIFY_RELAY_GATEWAY_INTERNAL_TIMEOUT_SECONDS", "45"))
    transport = httpx.ASGITransport(app=request.app)
    return httpx.AsyncClient(transport=transport, base_url="http://relay.internal", timeout=timeout)

@router.post("/v1/dify/relay")
async def dify_relay_gateway(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
    conversation_hash: Optional[str] = Header(default=None, alias="X-Conversation-Hash"),
):
    """Single Dify -> Railway facade over the existing Relay API.

    Additive only: it calls the already deployed /v1/jobs handlers inside the
    same ASGI app. relay-worker, Supabase persistence and legacy API contracts
    remain unchanged.
    """
    try:
        envelope = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_json: {exc}")
    if not isinstance(envelope, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")

    operation = str(envelope.get("operation") or "").strip().lower()
    headers = _forward_headers(authorization, idempotency_key, tenant_id, conversation_hash)

    async with await _internal_client(request) as client:
        if operation == "execute":
            job_request = envelope.get("request")
            if not isinstance(job_request, dict):
                raise HTTPException(status_code=400, detail="execute.request must be an object")

            if "Idempotency-Key" not in headers:
                canonical = json.dumps(job_request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                prefix = "dify" if str(job_request.get("stage") or "") == "normal_inference" else "fusion"
                headers["Idempotency-Key"] = f"{prefix}-gateway-{hashlib.sha256(canonical.encode()).hexdigest()}"
            if "X-Tenant-Id" not in headers and job_request.get("tenant_id"):
                headers["X-Tenant-Id"] = str(job_request.get("tenant_id"))
            if "X-Conversation-Hash" not in headers and job_request.get("conversation_hash"):
                headers["X-Conversation-Hash"] = str(job_request.get("conversation_hash"))

            resp = await client.post("/v1/jobs", json=job_request, headers=headers)
            obj = _json_or_text(resp)
            state = str(obj.get("status") or "failed") if isinstance(obj, dict) else "failed"
            payload = {
                "ok": bool(200 <= resp.status_code < 300 and isinstance(obj, dict) and obj.get("job_id")),
                "operation": "execute",
                "state": state,
                "status": state,
                "job_id": str(obj.get("job_id") or "") if isinstance(obj, dict) else "",
                "relay_session_id": str(obj.get("relay_session_id") or "") if isinstance(obj, dict) else "",
                "upstream_status": resp.status_code,
                "error": obj.get("error") if isinstance(obj, dict) else obj,
            }
            return JSONResponse(status_code=resp.status_code, content=payload)

        if operation in {"status", "result"}:
            job_id = str(envelope.get("job_id") or "").strip()
            if not job_id:
                raise HTTPException(status_code=400, detail=f"{operation}.job_id is required")

            if operation == "status":
                resp = await client.get(f"/v1/jobs/{job_id}", headers=headers)
                obj = _json_or_text(resp)
                state = str(obj.get("status") or "failed") if isinstance(obj, dict) else "failed"
                payload = {
                    "ok": bool(200 <= resp.status_code < 300),
                    "operation": "status",
                    "state": state,
                    "status": state,
                    "job_id": job_id,
                    "relay_session_id": str(obj.get("relay_session_id") or "") if isinstance(obj, dict) else "",
                    "heartbeat_at": str(obj.get("heartbeat_at") or "") if isinstance(obj, dict) else "",
                    "upstream_status": resp.status_code,
                    "error": obj.get("error") if isinstance(obj, dict) else obj,
                }
                return JSONResponse(status_code=resp.status_code, content=payload)

            view = str(envelope.get("view") or "dify")
            resp = await client.get(f"/v1/jobs/{job_id}/result", params={"view": view}, headers=headers)
            obj = _json_or_text(resp)
            state = (
                str(obj.get("status") or ("succeeded" if 200 <= resp.status_code < 300 else "failed"))
                if isinstance(obj, dict) else "failed"
            )
            payload: Dict[str, Any] = {
                "ok": bool(200 <= resp.status_code < 300),
                "operation": "result",
                "state": state,
                "status": state,
                "job_id": job_id,
                "upstream_status": resp.status_code,
                "error": obj.get("error") if isinstance(obj, dict) else obj,
                "result": obj,
            }
            # Backward compatibility: current normal-inference parser reads
            # text/status directly from the top level.
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key not in payload:
                        payload[key] = value
            return JSONResponse(status_code=resp.status_code, content=payload)

    raise HTTPException(status_code=400, detail="operation must be execute, status, or result")
