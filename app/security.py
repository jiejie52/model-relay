import secrets

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings


relay_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="RelayBearer",
    description="Model Relay API bearer token",
)


def require_relay_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(relay_bearer),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Relay bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    provided = credentials.credentials.strip()
    expected = get_settings().relay_api_token.get_secret_value()

    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Relay bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_owner_headers(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_conversation_hash: str | None = Header(
        default=None,
        alias="X-Conversation-Hash",
    ),
) -> tuple[str, str]:
    if not x_tenant_id or not x_conversation_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-Id and X-Conversation-Hash are required",
        )

    return x_tenant_id, x_conversation_hash
