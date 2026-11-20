"""Explicit Dify V2 transport facade. Business workflows remain outside V2 Core."""
from uuid import UUID
import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from app.security import relay_bearer, require_relay_auth
from app.v2_models import SessionCreateRequest, SessionRequestCreate


class TrustedDifyEntry:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        # ASGI scope is server-owned; a similarly named client header is ignored.
        await self.app({**scope, "relay.entry_point": "/v1/dify/relay"}, receive, send)


def validated_payload(model, raw):
    if not isinstance(raw, dict): raise HTTPException(422, "Expected an object")
    unknown = set(raw) - set(model.model_fields)
    if unknown: raise HTTPException(422, {"code": "DIFY_V2_UNKNOWN_FIELDS", "fields": sorted(unknown)})
    try: return model.model_validate(raw).model_dump(mode="json", exclude_unset=True)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_input=False, include_url=False, include_context=False)) from exc


async def handle_dify_v2(request, envelope, authorization, key, tenant, conversation):
    require_relay_auth(await relay_bearer(request))
    if not tenant or not conversation: raise HTTPException(400, "Owner headers are required")
    operation = envelope.get("operation")
    headers = {"Authorization": authorization or "", "X-Tenant-Id": tenant,
               "X-Conversation-Hash": conversation}
    from app.compatibility.relay_gateway import _json_or_text
    from fastapi.responses import JSONResponse
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=TrustedDifyEntry(request.app)),
                                 base_url="http://relay.internal", timeout=45) as client:
        session_id = envelope.get("session_id")
        if operation == "execute":
            if not key: raise HTTPException(400, "V2 execute requires a stable Idempotency-Key")
            payload = validated_payload(SessionRequestCreate, envelope.get("request"))
            if session_id is None:
                session_key = envelope.get("session_key")
                if not isinstance(session_key, str) or not session_key or len(session_key) > 490:
                    raise HTTPException(400, "A stable session_key is required when creating a Session")
                session = envelope.get("session")
                if not isinstance(session, dict): raise HTTPException(422, "session must be an object")
                session = dict(session)
                if session.get("tenant_id", tenant) != tenant or session.get("conversation_hash", conversation) != conversation:
                    raise HTTPException(403, "Session owner conflicts with headers")
                session.update(tenant_id=tenant, conversation_hash=conversation)
                session = validated_payload(SessionCreateRequest, session)
                response = await client.post("/v2/sessions", json=session,
                    headers={**headers, "Idempotency-Key": "dify-v2:" + session_key})
                data = _json_or_text(response)
                if response.status_code >= 300: return JSONResponse(status_code=response.status_code, content=data)
                session_id = data["session_id"]
            try: session_id = str(UUID(str(session_id)))
            except ValueError as exc: raise HTTPException(422, "Invalid session_id") from exc
            response = await client.post(f"/v2/sessions/{session_id}/requests", json=payload,
                                         headers={**headers, "Idempotency-Key": key})
        elif operation in {"status", "result"}:
            try:
                session_id = str(UUID(str(session_id)))
                request_id = str(UUID(str(envelope.get("request_id"))))
            except ValueError as exc: raise HTTPException(422, "V2 queries require session_id and request_id") from exc
            suffix = "/result" if operation == "result" else ""
            response = await client.get(f"/v2/sessions/{session_id}/requests/{request_id}{suffix}", headers=headers)
        else: raise HTTPException(400, "operation must be execute, status, or result")
        data = _json_or_text(response)
        # Stable IDs are the real Core records, never generated gateway aliases.
        content = {"execution_contract": "v2", "operation": operation,
                   "ok": response.status_code < 300, "session_id": session_id,
                   "request_id": envelope.get("request_id"), "payload": data}
        if isinstance(data, dict):
            for field in ("session_id", "request_id", "job_id", "status", "result", "error"):
                if field in data: content[field] = data[field]
        return JSONResponse(status_code=response.status_code, content=content)
