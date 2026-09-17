from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .compat.dify_gateway import router as dify_compat_router
from .compat.v1_jobs import router as v1_compat_router
from .config import get_settings
from .core_api import router as core_v2_router
from .core_service import CoreError, RelayCoreService
from .providers.registry import ProviderRegistry
from .repository import RelayRepository
from .supabase import SupabaseBackend


settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("model-relay-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    backend = SupabaseBackend(settings)
    repo = RelayRepository(backend, settings)
    providers = ProviderRegistry(settings)
    core_service = RelayCoreService(backend, repo, providers, settings)

    app.state.settings = settings
    app.state.backend = backend
    app.state.repo = repo
    app.state.providers = providers
    app.state.core_service = core_service
    try:
        yield
    finally:
        await backend.close()


app = FastAPI(
    title="Model Relay API",
    version="0.3.0-session-core-kimi",
    lifespan=lifespan,
)


def _v2_error_envelope(request: Request, *, code: str, message: str, details: Any = None) -> dict[str, Any]:
    request_id = request.headers.get("X-Request-Id") or request.headers.get("x-request-id")
    error: dict[str, Any] = {
        "origin": "relay",
        "relay_code": code,
        "relay_message": message,
    }
    if details is not None:
        error["details"] = details
    return {
        "schema_version": "relay-envelope/2.0",
        "request_id": request_id or f"req_{uuid4().hex}",
        "session": None,
        "job": None,
        "result": None,
        "error": error,
    }


@app.exception_handler(CoreError)
async def relay_core_error_handler(request: Request, exc: CoreError):
    if exc.error_meta:
        envelope = _v2_error_envelope(request, code=exc.code, message=exc.message)
        envelope["error"] = exc.error_meta
        return JSONResponse(status_code=exc.status_code, content=envelope)
    return JSONResponse(
        status_code=exc.status_code,
        content=_v2_error_envelope(request, code=exc.code, message=exc.message),
    )


@app.exception_handler(RequestValidationError)
async def relay_validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/v2/"):
        return JSONResponse(
            status_code=422,
            content=_v2_error_envelope(
                request,
                code="REQUEST_VALIDATION_ERROR",
                message="Relay v2 request validation failed",
                details=[
                    {
                        "loc": list(item.get("loc") or []),
                        "msg": str(item.get("msg") or ""),
                        "type": str(item.get("type") or ""),
                    }
                    for item in exc.errors()
                ],
            ),
        )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(StarletteHTTPException)
async def relay_http_error_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/v2/"):
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code") or detail.get("relay_code") or "RELAY_HTTP_ERROR")
            message = str(detail.get("message") or detail.get("relay_message") or detail)
        else:
            code = "RELAY_HTTP_ERROR"
            message = str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=_v2_error_envelope(request, code=code, message=message),
        )
    return await http_exception_handler(request, exc)


# Core is provider-neutral. Legacy surfaces are mounted only at composition root.
app.include_router(core_v2_router)
app.include_router(v1_compat_router)
app.include_router(dify_compat_router)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "relay-api",
        "version": "0.3.0-session-core-kimi",
        "core_api": "relay-envelope/2.0",
        "compatibility": ["/v1/jobs", "/v1/dify/relay"],
    }
