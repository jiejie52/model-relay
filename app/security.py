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


def require_v2_owner(
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> tuple[str, str]:
    """V2 owner guard.

    Existing deployments use one server-to-server Relay token. Operators can bind
    that token to a tenant with RELAY_ALLOWED_TENANT_ID so X-Tenant-Id is not
    accepted solely on caller assertion. Conversation ownership is still enforced
    on every repository/RPC query.
    """
    tenant_id, conversation_hash = owner
    allowed = get_settings().relay_allowed_tenant_id
    if allowed and not secrets.compare_digest(tenant_id, allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Relay token is not authorized for this tenant",
        )
    return tenant_id, conversation_hash
