from __future__ import annotations

from app.protocol_adapters.base import ProviderHTTPError, ProviderRequestError
from app.canonical_schema import StructuredOutputError
from app.supabase import SupabaseError


def error_source(exc: BaseException) -> str:
    if isinstance(exc, (ProviderHTTPError, ProviderRequestError)):
        return "provider"
    if isinstance(exc, SupabaseError):
        return "supabase"
    if isinstance(exc, StructuredOutputError):
        return "relay_validation"
    if isinstance(exc, TimeoutError):
        return "relay_timeout"
    return "relay"
