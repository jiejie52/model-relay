import secrets

from fastapi import Header, HTTPException, status

from .config import get_settings


def require_relay_auth(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Relay bearer token",
        )

    provided = authorization[7:].strip()
    expected = get_settings().relay_api_token.get_secret_value()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Relay bearer token",
        )


def require_owner_headers(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_conversation_hash: str | None = Header(default=None, alias="X-Conversation-Hash"),
) -> tuple[str, str]:
    if not x_tenant_id or not x_conversation_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-Id and X-Conversation-Hash are required",
        )
    return x_tenant_id, x_conversation_hash
